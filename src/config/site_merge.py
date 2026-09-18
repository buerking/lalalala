# -*- coding: utf-8 -*-
"""将根配置与单站点覆盖合并，供多站点 Runner 使用。"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

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
    "dev_test",
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
    adapter = (site.get("adapter") or "surugaya").strip()
    out["_site"] = {
        "id": site_id,
        "adapter": adapter,
        "display_name": (site.get("display_name") or site_id).strip(),
        "manual_login_url": (site.get("manual_login_url") or "").strip(),
    }
    out["_log_namespace"] = site_id
    if adapter == "playbook":
        try:
            from src.config.playbook_path import ensure_playbook_importable

            ensure_playbook_importable()
            from playbook.loader import apply_playbook_to_merged

            apply_playbook_to_merged(out, site_id)
            pb = out.get("_playbook") if isinstance(out.get("_playbook"), dict) else {}
            if pb.get("display_name"):
                out["_site"]["display_name"] = str(pb.get("display_name")).strip() or site_id
            if pb.get("manual_login_url") and not out["_site"].get("manual_login_url"):
                out["_site"]["manual_login_url"] = str(pb.get("manual_login_url")).strip()
        except Exception as e:
            out["_playbook"] = {}
            out["_playbook_error"] = str(e)
    return out


def list_site_entries(global_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回已启用站点：先 config.yaml 的 sites[]，再安全追加 main/sites 里尚未出现的 playbook 站。"""
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
    seen = {(e.get("id") or "").strip() for e in result}
    try:
        from src.config.playbook_path import ensure_playbook_importable

        ensure_playbook_importable()
        from playbook.loader import scan_playbook_tab_entries

        extras = scan_playbook_tab_entries()
    except Exception:
        extras = []
    for entry in extras:
        sid = (entry.get("id") or "").strip()
        if not sid or sid in seen:
            continue
        result.append(entry)
        seen.add(sid)
    return result


def is_multi_site_mode(global_cfg: Dict[str, Any]) -> bool:
    return bool(list_site_entries(global_cfg))
