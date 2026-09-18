# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path


def main_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def sites_dir() -> Path:
    return main_dir() / "sites"


def site_yaml_path(site_id: str) -> Path:
    return sites_dir() / site_id / "site.yaml"


def machine_yaml_path(site_id: str) -> Path:
    return sites_dir() / site_id / "machine.yaml"


def template_site_yaml() -> Path:
    return main_dir() / "templates" / "site.yaml"
