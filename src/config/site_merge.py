# -*- coding: utf-8 -*-
"""将根配置与单站点覆盖合并，供多站点 Runner 使用。"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

_MERGE_KEYS = (
    "browser",
    "order_api",
    "scheduler",
    "logging",
    "payment",
    "feishu_webhook",
    "cart_page",
    "product_page",
    "order_page",
    "order_confirm_page",
    "cart_verification",
    "gui",
    "yahoo_fleamarket",
    "rakuten_books",
    "rakuten_ichiba",
    "login",
    "cloudflare",
)


def merge_site_config(global_cfg: Dict[str, Any], site: Dict[str, Any]) -> Dict[str, Any]:
    """
    深拷贝全局配置后应用站点覆盖；写入 _site、_log_namespace。
    不会在结果中保留顶层的 sites 列表。
    """
    out = copy.deepcopy(global_cfg)
    out.pop("sites", None)

    for key in _MERGE_KEYS:
        if key not in site or site[key] is None:
            continue
        patch = site[key]
        base = out.get(key)
        if isinstance(base, dict) and isinstance(patch, dict):
            merged = copy.deepcopy(base)
            merged.update(patch)
            out[key] = merged
        else:
            out[key] = copy.deepcopy(patch)

    site_id = (site.get("id") or "default").strip() or "default"
    out["_site"] = {
        "id": site_id,
        "adapter": (site.get("adapter") or "surugaya").strip(),
        "display_name": (site.get("display_name") or site_id).strip(),
        "manual_login_url": (site.get("manual_login_url") or "").strip(),
    }
    out["_log_namespace"] = site_id
    return out


def list_site_entries(global_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回 config['sites'] 中已启用的站点定义列表；若无 sites 则返回空列表（走单站点兼容模式）。"""
    raw = global_cfg.get("sites")
    if not raw or not isinstance(raw, list):
        return []
    result: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled", True) is False:
            continue
        result.append(entry)
    return result


def is_multi_site_mode(global_cfg: Dict[str, Any]) -> bool:
    return bool(list_site_entries(global_cfg))
