# -*- coding: utf-8 -*-
"""按站点隔离文件日志：所有带 config['_log_namespace'] 的 LoggerMixin 日志写入 site.{id}。"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def attach_site_file_logger(site_id: str, config: Dict[str, Any]) -> logging.Logger:
    """
    为 logging.getLogger(f"site.{site_id}") 附加轮转文件 Handler（若尚未附加）。
    使用 config['logging'] 的 level、目录、大小等。
    """
    log = logging.getLogger(f"site.{site_id}")
    log.setLevel(getattr(logging, (config.get("logging") or {}).get("level", "INFO")))
    log.propagate = False

    for h in log.handlers:
        if getattr(h, "_site_file_handler", False):
            return log

    log_cfg = config.get("logging") or {}
    log_dir = log_cfg.get("log_dir", "logs")
    if not os.path.isabs(log_dir):
        log_dir = str((_project_root() / log_dir).resolve())
    os.makedirs(log_dir, exist_ok=True)

    log_file = (log_cfg.get("log_file") or "order_processor.log").strip()
    base, ext = os.path.splitext(log_file)
    if not ext:
        ext = ".log"
    file_name = f"{base}_{site_id}{ext}"
    log_path = os.path.join(log_dir, file_name)

    fh = RotatingFileHandler(
        log_path,
        maxBytes=int(log_cfg.get("max_file_size_mb", 10)) * 1024 * 1024,
        backupCount=int(log_cfg.get("backup_count", 5)),
        encoding="utf-8",
    )
    fh._site_file_handler = True  # type: ignore[attr-defined]
    fmt = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


def detach_site_handlers(site_id: str) -> None:
    """移除站点 Logger 上的文件 Handler，避免重复叠加。"""
    log = logging.getLogger(f"site.{site_id}")
    keep: list = []
    for h in log.handlers:
        if getattr(h, "_site_file_handler", False):
            try:
                h.close()
            except Exception:
                pass
            continue
        keep.append(h)
    log.handlers = keep
