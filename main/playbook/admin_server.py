# -*- coding: utf-8 -*-
"""GUI 按钮启停本机 FastAPI 配置后台。"""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from typing import Optional, Tuple

_log = logging.getLogger("playbook.admin")

HOST = "127.0.0.1"
PORT = 8787

_lock = threading.Lock()
_server = None
_thread: Optional[threading.Thread] = None


def is_running() -> bool:
    with _lock:
        srv = _server
    if srv is None:
        return False
    started = getattr(srv, "started", False)
    should_exit = getattr(srv, "should_exit", False)
    return bool(started) and not should_exit


def base_url() -> str:
    return "http://%s:%s/" % (HOST, PORT)


def start(open_browser: bool = True) -> Tuple[bool, str]:
    """启动后台。已在跑则只打开浏览器。"""
    global _server, _thread
    with _lock:
        if _server is not None and getattr(_server, "started", False) and not getattr(
            _server, "should_exit", False
        ):
            url = base_url()
            if open_browser:
                _open(url)
            return True, "配置后台已在运行: %s" % url
        try:
            import uvicorn
            from playbook.admin_app import create_app
        except Exception as e:
            return False, "无法启动配置后台（请 pip install -r main/requirements.txt）: %s" % e

        config = uvicorn.Config(
            create_app(),
            host=HOST,
            port=PORT,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        _server = uvicorn.Server(config)
        _thread = threading.Thread(target=_server.run, name="playbook-admin", daemon=True)
        _thread.start()

    url = base_url()
    deadline = time.time() + 8
    while time.time() < deadline:
        if getattr(_server, "started", False):
            if open_browser:
                _open(url)
            return True, "配置后台已打开: %s" % url
        if _thread is not None and not _thread.is_alive():
            return False, "配置后台线程已退出，端口 %s 可能被占用" % PORT
        time.sleep(0.1)
    if open_browser:
        _open(url)
    return True, "配置后台已启动（等待端口）: %s" % url


def stop() -> Tuple[bool, str]:
    global _server, _thread
    with _lock:
        srv = _server
        th = _thread
        if srv is None:
            return True, "配置后台未运行"
        srv.should_exit = True
        srv.force_exit = True
    if th is not None:
        th.join(timeout=4)
    with _lock:
        _server = None
        _thread = None
    return True, "配置后台已关闭"


def request_stop() -> None:
    """供网页内「关闭」按钮调用。"""
    try:
        stop()
    except Exception as e:
        _log.warning("配置后台关闭失败: %s", e)


def _open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as e:
        _log.warning("无法打开浏览器: %s", e)
