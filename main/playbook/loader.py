# -*- coding: utf-8 -*-
"""读取 / 校验 / 列出 main/sites/*/site.yaml。加载失败不抛给 GUI。"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from playbook.paths import machine_yaml_path, site_yaml_path, sites_dir, template_site_yaml

_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_log = logging.getLogger("playbook.loader")


def is_valid_site_id(site_id: str) -> bool:
    return bool(site_id and _ID_RE.match(site_id))


def _read_yaml(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.is_file():
        return None, "文件不存在: %s" % path
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as e:
        return None, "%s: %s" % (path, e)
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, "%s 根节点必须是 mapping" % path
    return data, None


def load_site_yaml(site_id: str) -> Tuple[Dict[str, Any], Optional[str]]:
    if not is_valid_site_id(site_id):
        return {}, "非法站点 id: %s" % site_id
    data, err = _read_yaml(site_yaml_path(site_id))
    if err:
        return {}, err
    out = dict(data or {})
    out.setdefault("id", site_id)
    return out, None


def load_machine_yaml(site_id: str) -> Dict[str, Any]:
    data, err = _read_yaml(machine_yaml_path(site_id))
    if err or not data:
        return {}
    return data


def list_playbook_site_ids() -> List[str]:
    root = sites_dir()
    if not root.is_dir():
        return []
    ids: List[str] = []
    try:
        for p in sorted(root.iterdir()):
            if not p.is_dir():
                continue
            if not is_valid_site_id(p.name):
                continue
            if (p / "site.yaml").is_file():
                ids.append(p.name)
    except Exception as e:
        _log.warning("扫描 main/sites 失败，已跳过新站: %s", e)
    return ids


def scan_playbook_tab_entries() -> List[Dict[str, Any]]:
    """
    给 GUI 追加 Tab 用的最小站点定义。
    任意单站 YAML 损坏只跳过该站。
    """
    entries: List[Dict[str, Any]] = []
    for sid in list_playbook_site_ids():
        data, err = load_site_yaml(sid)
        if err:
            _log.warning("跳过新站 %s: %s", sid, err)
            continue
        if data.get("enabled", True) is False:
            continue
        entries.append(
            {
                "id": sid,
                "adapter": "playbook",
                "display_name": str(data.get("display_name") or sid).strip() or sid,
                "enabled": True,
                "manual_login_url": str(data.get("manual_login_url") or "").strip(),
            }
        )
    return entries


def default_site_template(site_id: str = "new_site") -> Dict[str, Any]:
    path = template_site_yaml()
    data, err = _read_yaml(path)
    if err or not data:
        data = {"id": site_id, "display_name": site_id, "adapter": "playbook"}
    out = copy.deepcopy(data)
    out["id"] = site_id
    if not out.get("display_name"):
        out["display_name"] = site_id
    return out


def save_site_yaml(site_id: str, data: Dict[str, Any]) -> Optional[str]:
    if not is_valid_site_id(site_id):
        return "非法站点 id: %s" % site_id
    payload = dict(data or {})
    payload["id"] = site_id
    payload["adapter"] = "playbook"
    dest = site_yaml_path(site_id)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        dest.write_text(text, encoding="utf-8")
    except Exception as e:
        return "写入失败: %s" % e
    return None


def apply_playbook_to_merged(merged: Dict[str, Any], site_id: str) -> Dict[str, Any]:
    """
    把 site.yaml + machine.yaml 叠到已合并的站点 config 上。
    账密不从 YAML 覆盖（只认 config.yaml 的 login）。
    """
    pb, err = load_site_yaml(site_id)
    if err:
        merged["_playbook"] = {}
        merged["_playbook_error"] = err
        _log.warning("playbook 加载失败 %s: %s", site_id, err)
        return merged

    merged["_playbook"] = pb
    merged.pop("_playbook_error", None)

    ident = pb.get("identity") if isinstance(pb.get("identity"), dict) else {}
    pc_mark = str(ident.get("pc_mark") or pb.get("pc_mark") or site_id).strip()
    group_ids = ident.get("group_ids")
    if group_ids is None:
        group_ids = pb.get("group_ids")
    store_name = str(ident.get("store_name") or pb.get("store_name") or site_id).strip()
    credit = str(
        ident.get("credit_card")
        or pb.get("credit_card")
        or (merged.get("payment") or {}).get("add_no_credit_card")
        or ""
    ).strip()

    api = dict(merged.get("order_api") or {})
    if pc_mark:
        api["pc_mark"] = pc_mark
    if isinstance(group_ids, list) and group_ids:
        api["get_order_list_group_ids"] = group_ids
    tpl = str(ident.get("purchase_url_template") or pb.get("purchase_url_template") or "").strip()
    if tpl:
        api["purchase_url_template"] = tpl
    merged["order_api"] = api

    pay = dict(merged.get("payment") or {})
    if credit:
        pay["add_no_credit_card"] = credit
    merged["payment"] = pay
    merged["_playbook_store_name"] = store_name

    login_url = str(pb.get("manual_login_url") or "").strip()
    if login_url:
        site_meta = dict(merged.get("_site") or {})
        site_meta["manual_login_url"] = login_url
        merged["_site"] = site_meta

    browser = dict(merged.get("browser") or {})
    if not str(browser.get("user_data_dir") or "").strip():
        browser["user_data_dir"] = "data/chrome_user_data_%s" % site_id
    machine = load_machine_yaml(site_id)
    mb = machine.get("browser") if isinstance(machine.get("browser"), dict) else {}
    for k, v in mb.items():
        if v is not None:
            browser[k] = v
    merged["browser"] = browser
    return merged
