# -*- coding: utf-8 -*-
"""兼容旧「打开配置后台」：流程编辑已并入 8790 监控页，不再单独开 8787。"""

from __future__ import annotations

import logging
import threading
import webbrowser
from typing import Optional, Tuple

_log = logging.getLogger("playbook.admin")

HOST = "127.0.0.1"
PORT = 8787

_lock = threading.Lock()
_server = None
_thread: Optional[threading.Thread] = None


def is_running() -> bool:
    try:
        from src.monitor.server import is_running as monitor_running

        return monitor_running()
    except Exception:
        return False


def base_url() -> str:
    try:
        from src.monitor.server import base_urls

        urls = base_urls()
        return urls[0] if urls else "http://127.0.0.1:8790/"
    except Exception:
        return "http://127.0.0.1:8790/#playbook"


def start(open_browser: bool = True) -> Tuple[bool, str]:
    """打开合并后的监控页「自动下单流程」标签。"""
    _stop_legacy_8787()
    try:
        from src.config.config_loader import ConfigLoader
        from src.monitor.server import base_urls, current_token, start as monitor_start
    except Exception as e:
        return False, "无法打开合并后台: %s" % e
    cfg = {}
    try:
        cfg = ConfigLoader.get_config() or {}
    except Exception:
        pass
    ok, msg = monitor_start(cfg, open_browser=False)
    if not ok:
        return False, msg
    urls = base_urls()
    token = current_token()
    home = (urls[0] if urls else "http://127.0.0.1:8790/").rstrip("/") + "/"
    url = home + "?token=" + token + "#playbook" if token else home + "#playbook"
    if open_browser:
        _open(url)
    return True, "流程编辑已并入监控页: %s" % url


def stop() -> Tuple[bool, str]:
    _stop_legacy_8787()
    return True, "流程编辑已并入监控页，无需单独关闭（关程序时与监控页一起停）"


def request_stop() -> None:
    try:
        stop()
    except Exception as e:
        _log.warning("关闭旧配置后台失败: %s", e)


def _stop_legacy_8787() -> None:
    """若本进程里还留着旧 8787 uvicorn，关掉。不碰 8790。"""
    global _server, _thread
    with _lock:
        srv = _server
        th = _thread
        if srv is None:
            return
        srv.should_exit = True
        srv.force_exit = True
    if th is not None:
        th.join(timeout=4)
    with _lock:
        _server = None
        _thread = None


def _open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as e:
        _log.warning("无法打开浏览器: %s", e)
