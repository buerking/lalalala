# -*- coding: utf-8 -*-
"""配置后台 dry-run：真开浏览器，走到付款关键按钮前停止。"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("playbook.dry_run")
_lock = threading.Lock()
_browsers: Dict[str, Any] = {}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ensure_src_importable() -> None:
    root = str(_project_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def build_dry_run_order(site_data: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    dr = site_data.get("dry_run") if isinstance(site_data.get("dry_run"), dict) else {}
    raw = dict(dr.get("order") or {}) if isinstance(dr.get("order"), dict) else {}
    if overrides and isinstance(overrides, dict):
        raw.update({k: v for k, v in overrides.items() if v not in (None, "")})
    url = str(raw.get("url") or raw.get("goods_url") or "").strip()
    if not url:
        raise ValueError("请先填写测试订单的商品 URL（dry_run.order.url）")
    try:
        qty = max(1, int(raw.get("quantity") or raw.get("goods_number") or 1))
    except Exception:
        qty = 1
    try:
        price = float(raw.get("price") or raw.get("goods_price") or 0)
    except Exception:
        price = 0.0
    order_id = str(raw.get("order_id") or "DRYRUN").strip() or "DRYRUN"
    goods_id = str(raw.get("goods_id") or "1").strip() or "1"
    goods_no = str(raw.get("goods_no") or "").strip()
    return {
        "order_id": order_id,
        "order_no": str(raw.get("order_no") or order_id),
        "user_id": str(raw.get("user_id") or "dryrun"),
        "mark": str(raw.get("mark") or "dryrun-mark"),
        "secret": str(raw.get("secret") or "dryrun-secret"),
        "products": [
            {
                "url": url,
                "goods_id": goods_id,
                "goods_no": goods_no,
                "quantity": qty,
                "price": price,
                "name": str(raw.get("name") or "dry-run"),
            }
        ],
    }


def _site_entry(global_cfg: Dict[str, Any], site_id: str) -> Dict[str, Any]:
    for entry in global_cfg.get("sites") or []:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip() == site_id:
            return dict(entry)
    return {
        "id": site_id,
        "adapter": "playbook",
        "enabled": True,
        "display_name": site_id,
    }


def _prepare_config(site_id: str) -> Dict[str, Any]:
    _ensure_src_importable()
    from src.config.config_loader import ConfigLoader
    from src.config.site_merge import merge_site_config

    global_cfg = ConfigLoader.load_config()
    merged = merge_site_config(global_cfg, _site_entry(global_cfg, site_id))
    browser = dict(merged.get("browser") or {})
    browser["headless"] = False
    merged["browser"] = browser
    merged["_playbook_dry_run"] = True
    merged["_dry_run_mock"] = True
    dev = dict(merged.get("dev_test") or {})
    dev["enabled"] = True
    dev["stop_before_purchase"] = True
    dev["skip_callbacks"] = True
    dev["skip_feishu"] = True
    merged["dev_test"] = dev
    return merged


class _MemHandler(logging.Handler):
    def __init__(self, buf: List[str]):
        super().__init__()
        self.buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.append(self.format(record))
        except Exception:
            pass


def _attach_log(buf: List[str]) -> Tuple[logging.Handler, List[logging.Logger]]:
    handler = _MemHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    loggers = [
        logging.getLogger("playbook.dry_run"),
        logging.getLogger("playbook"),
        logging.getLogger("src.order"),
        logging.getLogger("src.browser"),
    ]
    for lg in loggers:
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)
    return handler, loggers


def _detach_log(handler: logging.Handler, loggers: List[logging.Logger]) -> None:
    for lg in loggers:
        try:
            lg.removeHandler(handler)
        except Exception:
            pass


def _browser_alive(manager) -> bool:
    if manager is None:
        return False
    try:
        driver = manager.get_driver()
        return bool(driver.window_handles)
    except Exception:
        return False


def stop_dry_run_browser(site_id: str) -> str:
    with _lock:
        bm = _browsers.pop(site_id, None)
    if bm is None:
        return "没有正在运行的试运行浏览器"
    try:
        bm.stop()
    except Exception as e:
        return "关闭浏览器失败: %s" % e
    return "已关闭试运行浏览器"


def run_playbook_dry_run(
    site_id: str, order_overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    from playbook.loader import load_site_yaml
    from playbook.processor import PlaybookOrderProcessor

    data, err = load_site_yaml(site_id)
    if err:
        raise ValueError(err)
    order = build_dry_run_order(data, order_overrides)
    logs: List[str] = []
    handler, loggers = _attach_log(logs)
    url = ""
    summary: Dict[str, Any] = {}
    try:
        merged = _prepare_config(site_id)
        _ensure_src_importable()
        from src.browser.browser_manager import BrowserManager

        with _lock:
            bm = _browsers.get(site_id)
            if not _browser_alive(bm):
                if bm is not None:
                    try:
                        bm.stop()
                    except Exception:
                        pass
                bm = BrowserManager(merged)
                try:
                    bm.start()
                except Exception as e:
                    raise RuntimeError(
                        "无法启动浏览器（该站 Chrome 用户目录可能被 GUI Tab 占用，请先停止对应站点）: %s"
                        % e
                    )
                _browsers[site_id] = bm
        processor = PlaybookOrderProcessor(merged, bm)
        ok, summary = processor.process_order(order)
        try:
            url = bm.get_driver().current_url or ""
        except Exception:
            url = ""
        return {
            "ok": bool(ok),
            "message": str((summary or {}).get("failure_reason") or ("试运行完成" if ok else "试运行失败")),
            "url": url,
            "summary": summary,
            "order": {
                "order_id": order.get("order_id"),
                "url": (order.get("products") or [{}])[0].get("url"),
            },
            "logs": logs[-80:],
        }
    finally:
        _detach_log(handler, loggers)
        _log.info("dry-run 结束 site=%s url=%s", site_id, url or "-")
