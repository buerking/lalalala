# -*- coding: utf-8 -*-
"""GUI 启停局域网监控服务（默认 0.0.0.0:8790，供 ZeroTier 访问）。"""

from __future__ import annotations

import logging
import secrets
import socket
import threading
import time
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple

_log = logging.getLogger("monitor")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8790

_lock = threading.Lock()
_server = None
_thread: Optional[threading.Thread] = None
_token = ""
_bind_host = DEFAULT_HOST
_bind_port = DEFAULT_PORT


def current_token() -> str:
    with _lock:
        return _token


def is_running() -> bool:
    with _lock:
        srv = _server
    if srv is None:
        return False
    return bool(getattr(srv, "started", False)) and not bool(getattr(srv, "should_exit", False))


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _token_file() -> Path:
    return _project_root() / "data" / "monitor_token.txt"


def resolve_token(config: Optional[dict] = None) -> str:
    sec = (config or {}).get("monitor") if isinstance((config or {}).get("monitor"), dict) else {}
    cfg_token = str((sec or {}).get("token") or "").strip()
    if cfg_token:
        return cfg_token
    path = _token_file()
    try:
        if path.is_file():
            saved = path.read_text(encoding="utf-8").strip()
            if saved:
                return saved
    except Exception:
        pass
    token = secrets.token_urlsafe(8)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
    except Exception as e:
        _log.warning("无法写入监控口令文件: %s", e)
    return token


def lan_urls(port: int) -> List[str]:
    urls = ["http://127.0.0.1:%s/" % port]
    seen = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip or ip in seen or ip.startswith("127."):
                continue
            seen.add(ip)
            urls.append("http://%s:%s/" % (ip, port))
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip not in seen and not ip.startswith("127."):
            urls.append("http://%s:%s/" % (ip, port))
    except Exception:
        pass
    return list(dict.fromkeys(urls))


def base_urls() -> List[str]:
    with _lock:
        port = _bind_port
    return lan_urls(port)


def start(config: Optional[dict] = None, open_browser: bool = False) -> Tuple[bool, str]:
    global _server, _thread, _token, _bind_host, _bind_port
    sec = (config or {}).get("monitor") if isinstance((config or {}).get("monitor"), dict) else {}
    if sec.get("enabled") is False:
        return False, "监控页已在配置中关闭（monitor.enabled=false）"
    host = str(sec.get("host") or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(sec.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    token = resolve_token(config)

    with _lock:
        if _server is not None and getattr(_server, "started", False) and not getattr(
            _server, "should_exit", False
        ):
            urls = lan_urls(_bind_port)
            msg = "监控页已在运行: %s  口令已设置" % "  |  ".join(urls)
            if open_browser:
                _open(urls[0])
            return True, msg
        try:
            import uvicorn
            from src.monitor.app import create_app
        except Exception as e:
            return False, "无法启动监控页（请 pip install fastapi uvicorn）: %s" % e

        _token = token
        _bind_host = host
        _bind_port = port
        cfg = uvicorn.Config(
            create_app(),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        _server = uvicorn.Server(cfg)
        _thread = threading.Thread(target=_server.run, name="monitor-web", daemon=True)
        _thread.start()

    urls = lan_urls(port)
    deadline = time.time() + 8
    while time.time() < deadline:
        if getattr(_server, "started", False):
            if open_browser:
                _open(urls[0])
            return True, _ready_msg(urls, token)
        if _thread is not None and not _thread.is_alive():
            return False, "监控页线程已退出，端口 %s 可能被占用" % port
        time.sleep(0.1)
    if open_browser:
        _open(urls[0])
    return True, _ready_msg(urls, token)


def stop() -> Tuple[bool, str]:
    global _server, _thread
    with _lock:
        srv = _server
        th = _thread
        if srv is None:
            return True, "监控页未运行"
        srv.should_exit = True
        srv.force_exit = True
    if th is not None:
        th.join(timeout=4)
    with _lock:
        _server = None
        _thread = None
    return True, "监控页已关闭"


def _ready_msg(urls: List[str], token: str) -> str:
    return (
        "监控页已打开。本机: %s  ZeroTier/局域网请用: %s  口令: %s"
        % (urls[0], "  ".join(urls[1:] or urls[:1]), token)
    )


def _open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as e:
        _log.warning("无法打开浏览器: %s", e)
