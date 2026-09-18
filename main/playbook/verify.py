# -*- coding: utf-8 -*-
"""每一步做完后的到达校验：目标 URL、标题、页面文案。"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from playbook.locator import page_has_any_text


def _lines(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    out = []
    for x in value:
        s = str(x or "").strip()
        if s:
            out.append(s)
    return out


def page_title(driver) -> str:
    try:
        return (driver.title or "").strip()
    except Exception:
        return ""


def current_url(driver) -> str:
    try:
        return (driver.current_url or "").strip()
    except Exception:
        return ""


def has_configured_arrived(arrived: Any) -> bool:
    if not isinstance(arrived, dict):
        return False
    return bool(
        _lines(arrived.get("url_contains"))
        or _lines(arrived.get("title_contains"))
        or _lines(arrived.get("texts"))
    )


def wait_arrived(
    driver,
    arrived: Any,
    *,
    timeout: float = 20.0,
    item_id: str = "",
    logger=None,
    step_name: str = "",
) -> Tuple[bool, str]:
    """
    点击后等待进入目标页。
    url / title / texts 填了的类型必须各自命中至少一条；没填的类型不检查。
    全部未填：返回未配置，由调用方决定是否当成失败。
    """
    spec = arrived if isinstance(arrived, dict) else {}
    urls = [x.replace("{item_id}", item_id or "") for x in _lines(spec.get("url_contains"))]
    titles = _lines(spec.get("title_contains"))
    texts = _lines(spec.get("texts"))
    if not urls and not titles and not texts:
        return False, "未配置「%s」到达校验（请填 url_contains / title_contains / texts）" % (step_name or "当前环节")

    timeout = float(spec.get("wait_seconds") or timeout or 20)
    deadline = time.time() + max(1.0, timeout)
    last = ""
    while time.time() < deadline:
        cur = current_url(driver)
        title = page_title(driver)
        url_ok = (not urls) or any(n in cur for n in urls)
        title_ok = (not titles) or any(n in title for n in titles)
        text_ok = (not texts) or page_has_any_text(driver, texts)
        last = "url=%s title=%s" % (cur, title)
        if url_ok and title_ok and text_ok:
            if logger:
                logger.info("playbook：%s 到达校验通过 %s", step_name or "环节", last)
            return True, last
        time.sleep(0.5)
    return False, "「%s」到达校验失败（%s）" % (step_name or "当前环节", last)
