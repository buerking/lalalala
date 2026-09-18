# -*- coding: utf-8 -*-
"""
一个接口里程碑里可以有多页。
接口要的字段（价格、合计、注文号等）在每一页都尝试抓，后抓到的覆盖先抓到的；
走完所有页再检查必填，缺了就失败，不要求运营事先知道数据在哪一页。
"""

from __future__ import annotations

from typing import Any, Dict, List

from playbook.extract import scrape_rows, scrape_text, scrape_yen
from playbook.verify import has_configured_arrived


def has_action(group: Any) -> bool:
    """下一步按钮是否真的配了选择器（空 primary 不算）。"""
    if not group:
        return False
    if isinstance(group, dict):
        if str(group.get("by") or "").strip() and str(group.get("value") or "").strip():
            return True
        for key in ("primary", "fallback"):
            items = group.get(key) or []
            if isinstance(items, dict):
                items = [items]
            for it in items:
                if isinstance(it, dict) and str(it.get("value") or "").strip():
                    return True
                if isinstance(it, str) and it.strip():
                    return True
        return False
    if isinstance(group, list):
        return any(has_action(x) for x in group)
    return bool(str(group).strip())


def normalize_pages(
    stage: Dict[str, Any],
    *,
    default_button_key: str = "next_button",
    treat_top_arrived_as_success: bool = False,
) -> List[Dict[str, Any]]:
    """pages[] 优先；没有则把旧的单页 arrived/button 收成一页，兼容已有 YAML。

    treat_top_arrived_as_success：旧版 confirm_pay.arrived 表示点完之后的成功页，
    不能当成第一页到达条件，否则会在点确定之前就去等成功页。
    """
    raw = stage.get("pages") if isinstance(stage, dict) else None
    if isinstance(raw, list) and any(isinstance(x, dict) for x in raw):
        return [dict(x) for x in raw if isinstance(x, dict)]
    arrived = {} if treat_top_arrived_as_success else ((stage or {}).get("arrived") or {})
    page = {
        "id": "page1",
        "title": "第1页",
        "arrived": arrived,
        "modal_ok": (stage or {}).get("modal_ok"),
        "checkboxes": (stage or {}).get("checkboxes") or [],
        "next_button": (stage or {}).get("next_button"),
        "skip_order_texts": (stage or {}).get("skip_order_texts") or [],
    }
    btn = (stage or {}).get("button")
    if btn and not page.get("next_button"):
        page[default_button_key] = btn
        page["next_button"] = btn
    return [page]


def collect_specs_of(stage: Dict[str, Any], extra: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    抓取定义挂在接口上，不挂在某一页：
    collect / extract / scrape(rows) 都会在每一页执行。
    """
    specs: Dict[str, Any] = {}
    for blob in (stage.get("collect"), stage.get("extract"), extra):
        if isinstance(blob, dict):
            specs.update(blob)
    scrape = stage.get("scrape")
    if isinstance(scrape, dict) and scrape.get("row"):
        specs["rows"] = scrape
    return specs


def harvest(driver, specs: Dict[str, Any], bag: Dict[str, Any], *, logger=None, page_name: str = "") -> List[str]:
    """在当前页尝试所有字段；有值就写入（后页覆盖前页）。返回本页新写入的键。"""
    if not specs:
        return []
    updated: List[str] = []
    for key, spec in specs.items():
        if not spec:
            continue
        if key == "rows":
            rows = scrape_rows(driver, spec, timeout=3, logger=logger)
            if rows:
                bag["rows"] = rows
                updated.append("rows")
                if logger:
                    logger.info("playbook：[%s] 抓到商品行 %s", page_name, len(rows))
            continue
        if key in ("price", "total", "goods_fee", "operate_fee"):
            yen = scrape_yen(driver, spec, timeout=2.5, logger=logger)
            if yen:
                bag[key] = yen
                updated.append(key)
                if logger:
                    logger.info("playbook：[%s] 抓到 %s=%s", page_name, key, yen)
            continue
        text = scrape_text(driver, spec, timeout=2.5, logger=logger)
        if text:
            bag[key] = text
            updated.append(key)
            if logger:
                logger.info("playbook：[%s] 抓到 %s=%s", page_name, key, text[:80])
    return updated


def missing_required(bag: Dict[str, Any], required: List[str]) -> List[str]:
    miss = []
    for key in required:
        if key in ("price", "amount"):
            if not (bag.get("price") or bag.get("total") or bag.get("goods_fee")):
                miss.append("price|total")
            continue
        if key == "total":
            if not (bag.get("total") or bag.get("price")):
                miss.append("total|price")
            continue
        if not bag.get(key):
            miss.append(key)
    # 去重保持顺序
    out: List[str] = []
    for x in miss:
        if x not in out:
            out.append(x)
    return out


def page_label(page: Dict[str, Any], index: int) -> str:
    return str(page.get("title") or page.get("id") or "第%s页" % (index + 1))


def need_arrived(page: Dict[str, Any]) -> bool:
    return has_configured_arrived(page.get("arrived"))
