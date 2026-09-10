# -*- coding: utf-8 -*-
"""本地开发假单测试：不打正式拉单接口，购买前停止。生产务必 enabled=false。"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set

_processed_lock = threading.Lock()


class DevTestStopBeforePurchase(Exception):
    """测试模式：已完成购买前步骤，跳过最终下单点击。"""


def _section(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    sec = config.get("dev_test")
    return sec if isinstance(sec, dict) else {}


def is_enabled(config: Optional[Dict[str, Any]]) -> bool:
    return bool(_section(config).get("enabled"))


def stop_before_purchase(config: Optional[Dict[str, Any]]) -> bool:
    if not is_enabled(config):
        return False
    return bool(_section(config).get("stop_before_purchase", True))


def skip_side_effects(config: Optional[Dict[str, Any]]) -> bool:
    """跳过 EDI 回调 / 工单等副作用。"""
    if not is_enabled(config):
        return False
    return bool(_section(config).get("skip_callbacks", True))


def skip_feishu(config: Optional[Dict[str, Any]]) -> bool:
    if not is_enabled(config):
        return False
    return bool(_section(config).get("skip_feishu", True))


def submit_bargain(config: Optional[Dict[str, Any]]) -> bool:
    """测试模式下是否真的点议价发送。默认 True（仍不是购买）。"""
    if not is_enabled(config):
        return True
    v = _section(config).get("submit_bargain")
    if v is None:
        return True
    return bool(v)


def skip_bargain_submit_verify(config: Optional[Dict[str, Any]]) -> bool:
    """
    本地测试：不要求进入 /negotiate，也不因未登录判定提交失败。
    生产路径不受影响。测试默认 True。
    """
    if not is_enabled(config):
        return False
    v = _section(config).get("skip_bargain_submit_verify")
    if v is None:
        return True
    return bool(v)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def resolve_path(config: Optional[Dict[str, Any]], rel: str) -> Path:
    p = Path(str(rel or "").strip())
    if not p.is_absolute():
        p = project_root() / p
    return p


def _adapter(config: Optional[Dict[str, Any]]) -> str:
    if not isinstance(config, dict):
        return ""
    return str((config.get("_site") or {}).get("adapter") or "").strip()


def orders_fixture_path(config: Optional[Dict[str, Any]]) -> Path:
    sec = _section(config)
    explicit = str(sec.get("orders_file") or "").strip()
    if explicit:
        return resolve_path(config, explicit)
    adapter = _adapter(config) or "yahoo_fleamarket"
    return project_root() / "tools" / "dev_test_fixtures" / ("%s_orders.json" % adapter)


def bargain_fixture_path(config: Optional[Dict[str, Any]]) -> Path:
    sec = _section(config)
    explicit = str(sec.get("bargain_orders_file") or "").strip()
    if explicit:
        return resolve_path(config, explicit)
    adapter = _adapter(config) or "yahoo_fleamarket"
    return project_root() / "tools" / "dev_test_fixtures" / (
        "%s_bargain_orders.json" % adapter
    )


def bargain_records_path(config: Optional[Dict[str, Any]]) -> Optional[str]:
    if not is_enabled(config):
        return None
    rel = str(_section(config).get("bargain_records_file") or "").strip()
    if not rel:
        rel = "data/dev_test_yahoo_bargain_records.json"
    return str(resolve_path(config, rel))


def processed_orders_path(config: Optional[Dict[str, Any]]) -> Path:
    """本地测试：已跑过的普通假单 ID，次轮不再重放。"""
    rel = str(_section(config).get("processed_orders_file") or "").strip()
    if not rel:
        rel = "data/dev_test_processed_orders.json"
    return resolve_path(config, rel)


def load_processed_order_ids(config: Optional[Dict[str, Any]]) -> Set[str]:
    if not is_enabled(config):
        return set()
    path = processed_orders_path(config)
    if not path.is_file():
        return set()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return set()
    raw = data.get("order_ids") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def mark_order_processed(config: Optional[Dict[str, Any]], order_id: Any) -> None:
    if not is_enabled(config):
        return
    oid = str(order_id or "").strip()
    if not oid:
        return
    path = processed_orders_path(config)
    with _processed_lock:
        ids = load_processed_order_ids(config)
        if oid in ids:
            return
        ids.add(oid)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "order_ids": sorted(ids),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)


def load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def set_enabled(config: Dict[str, Any], enabled: bool) -> None:
    if not isinstance(config, dict):
        return
    sec = config.setdefault("dev_test", {})
    if not isinstance(sec, dict):
        config["dev_test"] = {"enabled": bool(enabled)}
        return
    sec["enabled"] = bool(enabled)
