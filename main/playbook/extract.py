# -*- coding: utf-8 -*-
"""按 site.yaml 的 scrape 定义从页面/行内抽出文本、金额、库存、注文号。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from playbook.locator import find_all, find_first, read_element

_YEN_RE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*円")
_INT_RE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)")


def _as_spec(spec: Any) -> Dict[str, Any]:
    if not spec:
        return {}
    if isinstance(spec, dict) and (
        "primary" in spec or "fallback" in spec or spec.get("by")
    ):
        return {"locators": spec, "source": "text", "regex": spec.get("regex") or ""}
    if isinstance(spec, dict):
        loc = spec.get("locators") or spec.get("locator") or spec
        if not isinstance(loc, dict) and not isinstance(loc, list):
            loc = {}
        return {
            "locators": loc if ("primary" in (loc or {}) or "fallback" in (loc or {}) or (isinstance(loc, dict) and loc.get("by"))) else spec,
            "source": str(spec.get("source") or spec.get("from") or "text"),
            "attr": str(spec.get("attr") or ""),
            "regex": str(spec.get("regex") or ""),
            "group": spec.get("regex_group", 1),
        }
    if isinstance(spec, (list, str)):
        return {"locators": spec, "source": "text", "regex": ""}
    return {}


def apply_regex(text: str, pattern: str, group: Any = 1) -> str:
    if not pattern or not text:
        return (text or "").strip()
    try:
        m = re.search(pattern, text)
    except Exception:
        return text.strip()
    if not m:
        return ""
    try:
        g = int(group) if group not in (None, "") else 1
    except Exception:
        g = 1
    if m.lastindex and g <= m.lastindex:
        return (m.group(g) or "").strip()
    if "id" in m.groupdict():
        return (m.group("id") or "").strip()
    return (m.group(0) or "").strip()


def parse_yen(text: str) -> Optional[int]:
    if not text:
        return None
    hits = [_to_int(x) for x in _YEN_RE.findall(text)]
    hits = [x for x in hits if x]
    if hits:
        return hits[0]
    return _to_int(text)


def parse_int(text: str) -> Optional[int]:
    if not text:
        return None
    m = _INT_RE.search(text.replace("，", ","))
    if not m:
        return _to_int(text)
    return _to_int(m.group(1))


def _to_int(raw: str) -> Optional[int]:
    s = (raw or "").replace(",", "").replace("，", "").replace("円", "").strip()
    if not s.isdigit() and not (s.startswith("-") and s[1:].isdigit()):
        return None
    try:
        return int(s)
    except Exception:
        return None


def scrape_text(driver, spec: Any, *, root=None, timeout: float = 3.0, logger=None) -> str:
    cfg = _as_spec(spec)
    loc = cfg.get("locators") or spec
    if not loc:
        return ""
    el = find_first(driver, loc, timeout=timeout, logger=logger, root=root)
    if el is None:
        return ""
    raw = read_element(el, source=str(cfg.get("source") or "text"), attr=str(cfg.get("attr") or ""))
    return apply_regex(raw, str(cfg.get("regex") or ""), cfg.get("group", 1))


def scrape_yen(driver, spec: Any, *, root=None, timeout: float = 3.0, logger=None) -> Optional[int]:
    text = scrape_text(driver, spec, root=root, timeout=timeout, logger=logger)
    v = parse_yen(text)
    if v:
        return v
    if spec:
        try:
            blob = (root.text if root is not None else (driver.page_source or "")) or ""
        except Exception:
            blob = ""
        return parse_yen(blob)
    return None


def scrape_int(driver, spec: Any, *, root=None, timeout: float = 3.0, logger=None) -> Optional[int]:
    text = scrape_text(driver, spec, root=root, timeout=timeout, logger=logger)
    return parse_int(text)


def scrape_fields(driver, fields: Any, *, root=None, timeout: float = 3.0, logger=None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(fields, dict):
        return out
    for key, spec in fields.items():
        if key in ("row",):
            continue
        out[str(key)] = scrape_text(driver, spec, root=root, timeout=timeout, logger=logger)
    return out


def scrape_rows(driver, spec: Any, *, timeout: float = 4.0, logger=None) -> List[Dict[str, str]]:
    """
    cart.scrape:
      row: {primary/fallback}
      name/price/quantity/url/item_id: 相对行的选择器
    """
    if not isinstance(spec, dict):
        return []
    row_loc = spec.get("row")
    rows = find_all(driver, row_loc, timeout=timeout, logger=logger) if row_loc else []
    fields = spec.get("fields") if isinstance(spec.get("fields"), dict) else {
        k: v for k, v in spec.items() if k != "row"
    }
    out: List[Dict[str, str]] = []
    for row in rows:
        item = scrape_fields(driver, fields, root=row, timeout=1.2, logger=logger)
        if any(v for v in item.values()):
            out.append(item)
    return out
