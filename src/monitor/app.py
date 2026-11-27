# -*- coding: utf-8 -*-
"""ZeroTier / 局域网监控 API。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src.config.config_loader import ConfigLoader
from src.monitor import hub
from src.order.prohibited_item_guard import (
    keyword_editor_state,
    prohibited_items_from_editor,
)
from src.order.rakuten_ichiba_seller_rules import (
    editor_payload as seller_rules_payload,
    preview_intent as seller_rules_preview,
    save_seller_rules,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class KeywordsPayload(BaseModel):
    categories: Dict[str, Any] = Field(default_factory=dict)
    whitelist: Dict[str, Any] = Field(default_factory=dict)


class SellerRulesPayload(BaseModel):
    data: Dict[str, Any] = Field(default_factory=dict)


class SellerPreviewPayload(BaseModel):
    urls: Any = Field(default_factory=list)


def _cfg() -> Dict[str, Any]:
    try:
        return ConfigLoader.get_config() or {}
    except Exception:
        return {}


def expected_token() -> str:
    from src.monitor.server import current_token

    return current_token()


def _check_token(request: Request, x_monitor_token: Optional[str]) -> None:
    want = expected_token()
    if not want:
        return
    got = (x_monitor_token or "").strip() or str(request.query_params.get("token") or "").strip()
    if got != want:
        raise HTTPException(401, "口令错误")


def create_app() -> FastAPI:
    app = FastAPI(title="jpgoodbuy 后台", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path or ""
        if path in ("/", "/playbook", "/playbook/", "/api/health") or path.startswith(
            "/static/"
        ):
            return await call_next(request)
        if path == "/api/login" and request.method == "POST":
            return await call_next(request)
        try:
            _check_token(request, request.headers.get("x-monitor-token"))
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        return await call_next(request)

    @app.get("/")
    def index():
        html = STATIC_DIR / "monitor.html"
        if not html.is_file():
            raise HTTPException(500, "缺少 monitor.html")
        return FileResponse(html)

    @app.get("/playbook")
    @app.get("/playbook/")
    def playbook_index():
        try:
            from src.config.playbook_path import ensure_playbook_importable

            ensure_playbook_importable()
            from playbook.admin_app import playbook_index_file
        except Exception as e:
            raise HTTPException(500, "无法加载流程编辑器: %s" % e)
        html = playbook_index_file()
        if not html.is_file():
            raise HTTPException(500, "缺少 admin.html")
        return FileResponse(html)

    @app.get("/api/health")
    def health():
        return {"ok": True, "gui": hub.get_window() is not None}

    @app.post("/api/login")
    def login(body: Dict[str, Any]):
        token = str((body or {}).get("token") or "").strip()
        if expected_token() and token != expected_token():
            raise HTTPException(401, "口令错误")
        return {"ok": True}

    @app.get("/api/status")
    def status():
        try:
            sites = hub.site_snapshot()
        except Exception:
            sites = []
        return {
            "ok": True,
            "gui": hub.get_window() is not None,
            "sites": sites,
        }

    @app.post("/api/sites/{site_id}/start")
    def start_site(site_id: str):
        try:
            msg = hub.start_site(site_id)
        except Exception as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "message": msg}

    @app.post("/api/sites/{site_id}/stop")
    def stop_site(site_id: str):
        try:
            msg = hub.stop_site(site_id)
        except Exception as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "message": msg}

    @app.get("/api/keywords")
    def get_keywords():
        return keyword_editor_state(_cfg())

    @app.put("/api/keywords")
    def put_keywords(body: KeywordsPayload):
        section = prohibited_items_from_editor(body.categories, body.whitelist)
        old = _cfg().get("prohibited_items") if isinstance(_cfg().get("prohibited_items"), dict) else {}
        merged = dict(old)
        merged["extra_keywords"] = section.get("extra_keywords") or {}
        merged["disabled_keywords"] = section.get("disabled_keywords") or {}
        merged["keyword_whitelist"] = section.get("keyword_whitelist") or {}
        err = _write_prohibited_items(merged)
        if err:
            raise HTTPException(500, err)
        try:
            hub.apply_prohibited(merged)
        except Exception as e:
            raise HTTPException(500, "已写入文件但未能热更新 GUI: %s" % e)
        return {"ok": True, "prohibited_items": merged}

    @app.get("/api/rakuten-seller-rules")
    def get_seller_rules():
        return seller_rules_payload()

    @app.put("/api/rakuten-seller-rules")
    def put_seller_rules(body: SellerRulesPayload):
        try:
            saved = save_seller_rules(body.data or {})
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, str(e))
        return {"ok": True, **saved}

    @app.post("/api/rakuten-seller-rules/preview")
    def preview_seller_rules(body: SellerPreviewPayload):
        raw = body.urls
        if isinstance(raw, str):
            urls = [raw]
        elif isinstance(raw, list):
            urls = [str(x) for x in raw]
        else:
            urls = []
        return seller_rules_preview(urls)

    _mount_playbook(app)
    return app


def _mount_playbook(app: FastAPI) -> None:
    try:
        from src.config.playbook_path import ensure_playbook_importable

        ensure_playbook_importable()
        from playbook.admin_app import register_playbook_api

        register_playbook_api(app)
    except Exception as e:
        import logging

        logging.getLogger("monitor").warning("未挂载自动下单流程编辑器: %s", e)


def _write_prohibited_items(section: Dict[str, Any]) -> Optional[str]:
    path = getattr(ConfigLoader, "_config_path", None)
    if not path or not os.path.exists(path):
        return "未找到 config.yaml"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return "config.yaml 格式异常"
        cur = data.get("prohibited_items") if isinstance(data.get("prohibited_items"), dict) else {}
        cur = dict(cur)
        cur.update(section)
        data["prohibited_items"] = cur
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        ConfigLoader.reload_config()
        return None
    except Exception as e:
        return str(e)
