# -*- coding: utf-8 -*-
"""GUI 与监控页之间的桥梁。网页线程通过 root.after 调主窗口。"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

_log = logging.getLogger("monitor")

_window = None
_lock = threading.Lock()


def attach(window: Any) -> None:
    global _window
    with _lock:
        _window = window


def detach(window: Any = None) -> None:
    global _window
    with _lock:
        if window is None or _window is window:
            _window = None


def get_window() -> Any:
    with _lock:
        return _window


def run_on_ui(fn: Callable[[], Any], timeout: float = 25.0) -> Any:
    """在 Tk 主线程执行 fn，避免网页线程直接点 GUI。"""
    win = get_window()
    if win is None:
        raise RuntimeError("监控页未连接到 GUI（请在生产机打开本程序）")
    box: Dict[str, Any] = {}
    done = threading.Event()

    def wrap() -> None:
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e
        done.set()

    win.root.after(0, wrap)
    if not done.wait(timeout):
        raise TimeoutError("GUI 无响应，请看生产机窗口是否卡住")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def site_snapshot() -> List[Dict[str, Any]]:
    def work() -> List[Dict[str, Any]]:
        win = get_window()
        if win is None:
            return []
        return list(win.monitor_site_rows())

    try:
        return list(run_on_ui(work, timeout=8) or [])
    except Exception as e:
        _log.warning("读取站点状态失败: %s", e)
        return []


def start_site(site_id: str) -> str:
    def work() -> str:
        win = get_window()
        if win is None:
            raise RuntimeError("GUI 未连接")
        return win.monitor_start_site(site_id)

    return str(run_on_ui(work) or "ok")


def stop_site(site_id: str) -> str:
    def work() -> str:
        win = get_window()
        if win is None:
            raise RuntimeError("GUI 未连接")
        return win.monitor_stop_site(site_id)

    return str(run_on_ui(work) or "ok")


def apply_prohibited(section: Dict[str, Any]) -> None:
    def work() -> None:
        win = get_window()
        if win is None:
            raise RuntimeError("GUI 未连接")
        win.monitor_apply_prohibited(section)

    run_on_ui(work)
