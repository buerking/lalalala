# -*- coding: utf-8 -*-
"""选择器库：primary → fallback；不使用哈希 sc-* class 作为定位依据。"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement

# styled-components / CSS-in-JS 哈希 class，intake 明确要求不要当关键信息
_HASH_CLASS_RE = re.compile(r"(?:^|[.\s\[])sc-[0-9a-f]{4,}(?:-\d+)?", re.I)


def is_hashed_class_locator(loc: Dict[str, Any]) -> bool:
    by = str(loc.get("by") or "css").strip().lower()
    value = str(loc.get("value") or "")
    if by in ("css", "css_selector", "class"):
        return bool(_HASH_CLASS_RE.search(value))
    if by == "xpath" and _HASH_CLASS_RE.search(value):
        return True
    return False


def _as_list(group: Any) -> List[Dict[str, Any]]:
    if not group:
        return []
    if isinstance(group, dict) and ("primary" in group or "fallback" in group):
        out: List[Dict[str, Any]] = []
        for key in ("primary", "fallback"):
            items = group.get(key) or []
            if isinstance(items, dict):
                items = [items]
            for it in items:
                if isinstance(it, dict):
                    row = dict(it)
                    row["_src"] = key
                    out.append(row)
                elif isinstance(it, str) and it.strip():
                    out.append({"by": "css", "value": it.strip(), "_src": key})
        return out
    if isinstance(group, dict) and group.get("by"):
        return [dict(group)]
    if isinstance(group, list):
        rows: List[Dict[str, Any]] = []
        for it in group:
            if isinstance(it, dict):
                rows.append(dict(it))
            elif isinstance(it, str) and it.strip():
                rows.append({"by": "css", "value": it.strip()})
        return rows
    if isinstance(group, str) and group.strip():
        return [{"by": "css", "value": group.strip()}]
    return []


def iter_locators(group: Any) -> Iterable[Dict[str, Any]]:
    for loc in _as_list(group):
        yield loc


def to_selenium(loc: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    by = str(loc.get("by") or "css").strip().lower()
    value = str(loc.get("value") or "").strip()
    exact = bool(loc.get("exact", False))
    if not value:
        return None
    if by in ("css", "css_selector"):
        return By.CSS_SELECTOR, value
    if by == "id":
        return By.ID, value
    if by == "name":
        return By.NAME, value
    if by == "xpath":
        return By.XPATH, value
    if by in ("text", "link_text", "partial_text"):
        if by == "link_text" or (by == "text" and loc.get("tag") == "a" and exact):
            return By.LINK_TEXT, value
        if exact:
            xp = (
                "//*[normalize-space()=%s] | //*[@value=%s] | //*[@aria-label=%s]"
                % (_xp_lit(value), _xp_lit(value), _xp_lit(value))
            )
        else:
            xp = (
                "//*[contains(normalize-space(), %s)] | "
                "//*[contains(@value, %s)] | //*[contains(@aria-label, %s)]"
                % (_xp_lit(value), _xp_lit(value), _xp_lit(value))
            )
        return By.XPATH, xp
    if by in ("aria", "aria_label"):
        if exact:
            return By.CSS_SELECTOR, '[aria-label="%s"]' % value.replace('"', '\\"')
        return By.CSS_SELECTOR, '[aria-label*="%s"]' % value.replace('"', '\\"')
    if by == "href":
        return By.CSS_SELECTOR, 'a[href*="%s"]' % value.replace('"', '\\"')
    if by == "placeholder":
        return By.CSS_SELECTOR, '[placeholder*="%s"]' % value.replace('"', '\\"')
    if by == "role":
        return By.CSS_SELECTOR, '[role="%s"]' % value.replace('"', '\\"')
    return By.CSS_SELECTOR, value


def _xp_lit(s: str) -> str:
    if "'" not in s:
        return "'%s'" % s
    if '"' not in s:
        return '"%s"' % s
    parts = s.split("'")
    return "concat(%s)" % ", \"'\", ".join("'%s'" % p for p in parts)


def find_first(
    driver,
    group: Any,
    *,
    timeout: float = 6.0,
    logger=None,
    clickable: bool = False,
    root=None,
) -> Optional[WebElement]:
    """按 primary → fallback 依次找，超时均分到各条。哈希 class 直接跳过。"""
    locs = [x for x in iter_locators(group)]
    if not locs:
        return None
    ctx = root if root is not None else driver
    deadline = time.time() + max(0.2, float(timeout))
    last_note = ""
    while time.time() < deadline:
        for loc in locs:
            if is_hashed_class_locator(loc):
                last_note = "跳过哈希 class: %s" % loc.get("value")
                continue
            pair = to_selenium(loc)
            if not pair:
                continue
            by, value = pair
            try:
                els = ctx.find_elements(by, value)
            except Exception as e:
                last_note = "%s" % e
                continue
            for el in els:
                try:
                    if clickable and not el.is_enabled():
                        continue
                    if el.is_displayed() or clickable or root is not None:
                        return el
                except Exception:
                    continue
        time.sleep(0.25)
    if logger and last_note:
        try:
            logger.debug("定位未命中: %s", last_note)
        except Exception:
            pass
    return None


def find_all(driver, group: Any, *, timeout: float = 3.0, logger=None, root=None) -> List[WebElement]:
    locs = [x for x in iter_locators(group)]
    if not locs:
        return []
    ctx = root if root is not None else driver
    deadline = time.time() + max(0.1, float(timeout))
    found: List[WebElement] = []
    while time.time() < deadline:
        found = []
        for loc in locs:
            if is_hashed_class_locator(loc):
                continue
            pair = to_selenium(loc)
            if not pair:
                continue
            by, value = pair
            try:
                els = ctx.find_elements(by, value)
            except Exception:
                continue
            for el in els:
                try:
                    found.append(el)
                except Exception:
                    continue
            if found:
                return found
        time.sleep(0.2)
    return found


def read_element(el: WebElement, *, source: str = "text", attr: str = "") -> str:
    try:
        src = (source or "text").strip().lower()
        if src in ("href",):
            return (el.get_attribute("href") or "").strip()
        if src in ("value",):
            return (el.get_attribute("value") or "").strip()
        if src in ("attr", "attribute") and attr:
            return (el.get_attribute(attr) or "").strip()
        return " ".join((el.text or "").split())
    except Exception:
        return ""


def click_first(driver, group: Any, *, timeout: float = 6.0, logger=None, root=None) -> bool:
    el = find_first(
        driver, group, timeout=timeout, logger=logger, clickable=True, root=root
    )
    if el is None:
        return False
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    try:
        el.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False


def ensure_checked(driver, group: Any, *, timeout: float = 4.0, logger=None) -> bool:
    """勾选 checkbox/radio；已勾选则跳过。"""
    el = find_first(driver, group, timeout=timeout, logger=logger)
    if el is None:
        return False
    try:
        if el.is_selected():
            return True
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    try:
        el.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False


def fill_first(driver, group: Any, text: str, *, timeout: float = 6.0, logger=None) -> bool:
    el = find_first(driver, group, timeout=timeout, logger=logger)
    if el is None:
        return False
    try:
        el.clear()
    except Exception:
        pass
    try:
        el.send_keys(text)
        return True
    except Exception:
        try:
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));",
                el,
                text,
            )
            return True
        except Exception:
            return False


def page_has_any_text(driver, needles: List[str]) -> bool:
    if not needles:
        return False
    try:
        src = (driver.page_source or "") + " " + (driver.current_url or "")
    except Exception:
        src = ""
    return any(n and n in src for n in needles)
