# -*- coding: utf-8 -*-
"""把仓库 main/ 接到 import 路径，且不与根目录 main.py 抢模块名。"""

from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main_dir() -> Path:
    return project_root() / "main"


def sites_dir() -> Path:
    return main_dir() / "sites"


def ensure_playbook_importable() -> Path:
    """
    将 main/ 插入 sys.path，从而 import playbook。
    不要把项目根插到 path：根上有 main.py，会挡住 main/ 包。
    """
    d = str(main_dir())
    if d not in sys.path:
        sys.path.insert(0, d)
    return main_dir()
