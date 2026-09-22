# -*- coding: utf-8 -*-
"""本机站点配置后台（FastAPI）。由 GUI 按钮启停。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from playbook.loader import (
    default_site_template,
    is_valid_site_id,
    list_playbook_site_ids,
    load_site_yaml,
    save_site_yaml,
)
from playbook.paths import main_dir, site_yaml_path

STATIC_DIR = Path(__file__).resolve().parent / "static"


class SitePayload(BaseModel):
    data: Dict[str, Any] = Field(default_factory=dict)


class NewSitePayload(BaseModel):
    id: str
    display_name: str = ""


class DryRunPayload(BaseModel):
    order: Dict[str, Any] = Field(default_factory=dict)


def create_app() -> FastAPI:
    app = FastAPI(title="jpgoodbuy 站点配置", docs_url="/docs", redoc_url=None)

    @app.get("/")
    def index():
        html = STATIC_DIR / "admin.html"
        if not html.is_file():
            raise HTTPException(500, "缺少 admin.html")
        return FileResponse(html)

    @app.get("/api/health")
    def health():
        return {"ok": True, "main_dir": str(main_dir())}

    @app.get("/api/sites")
    def api_list_sites():
        items = []
        for sid in list_playbook_site_ids():
            data, err = load_site_yaml(sid)
            items.append(
                {
                    "id": sid,
                    "display_name": (data or {}).get("display_name") or sid,
                    "enabled": (data or {}).get("enabled", True),
                    "error": err,
                    "path": str(site_yaml_path(sid)),
                }
            )
        return {"sites": items}

    @app.get("/api/sites/{site_id}")
    def api_get_site(site_id: str):
        if not is_valid_site_id(site_id):
            raise HTTPException(400, "非法站点 id")
        data, err = load_site_yaml(site_id)
        if err:
            raise HTTPException(404, err)
        snippet = _config_snippet(site_id, data)
        return {"id": site_id, "data": data, "config_snippet": snippet}

    @app.put("/api/sites/{site_id}")
    def api_put_site(site_id: str, body: SitePayload):
        if not is_valid_site_id(site_id):
            raise HTTPException(400, "非法站点 id")
        payload = copy.deepcopy(body.data or {})
        payload["id"] = site_id
        payload["adapter"] = "playbook"
        err = save_site_yaml(site_id, payload)
        if err:
            raise HTTPException(400, err)
        return {"ok": True, "path": str(site_yaml_path(site_id))}

    @app.post("/api/sites")
    def api_new_site(body: NewSitePayload):
        sid = (body.id or "").strip()
        if not is_valid_site_id(sid):
            raise HTTPException(400, "id 须为字母开头的字母数字下划线")
        dest = site_yaml_path(sid)
        if dest.is_file():
            raise HTTPException(409, "站点已存在")
        data = default_site_template(sid)
        data["id"] = sid
        data["display_name"] = (body.display_name or sid).strip() or sid
        err = save_site_yaml(sid, data)
        if err:
            raise HTTPException(400, err)
        return {
            "ok": True,
            "id": sid,
            "config_snippet": _config_snippet(sid, data),
        }

    @app.get("/api/template")
    def api_template():
        return {"data": default_site_template("new_site")}

    @app.post("/api/shutdown")
    def api_shutdown():
        from playbook.admin_server import request_stop

        request_stop()
        return JSONResponse({"ok": True, "message": "正在关闭配置后台"})

    @app.post("/api/sites/{site_id}/dry-run")
    def api_dry_run(site_id: str, body: Optional[DryRunPayload] = None):
        if not is_valid_site_id(site_id):
            raise HTTPException(400, "非法站点 id")
        from playbook.dry_run import run_playbook_dry_run

        try:
            result = run_playbook_dry_run(
                site_id, (body.order if body else None) or {}
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, str(e))
        return result

    @app.post("/api/sites/{site_id}/dry-run/stop")
    def api_dry_run_stop(site_id: str):
        if not is_valid_site_id(site_id):
            raise HTTPException(400, "非法站点 id")
        from playbook.dry_run import stop_dry_run_browser

        msg = stop_dry_run_browser(site_id)
        return {"ok": True, "message": msg}

    return app


def _config_snippet(site_id: str, data: Dict[str, Any]) -> str:
    name = (data or {}).get("display_name") or site_id
    url = (data or {}).get("manual_login_url") or ""
    ident = (data or {}).get("identity") if isinstance((data or {}).get("identity"), dict) else {}
    pc = ident.get("pc_mark") or site_id
    groups = ident.get("group_ids") or []
    credit = ident.get("credit_card") or ""
    return (
        "- id: {id}\n"
        "  enabled: true\n"
        "  display_name: {name}\n"
        "  adapter: playbook\n"
        "  manual_login_url: {url}\n"
        "  login:\n"
        "    enabled: true\n"
        "    email: ''\n"
        "    password: ''\n"
        "  browser:\n"
        "    user_data_dir: data/chrome_user_data_{id}\n"
        "    keep_browser_open_on_stop: true\n"
        "  order_api:\n"
        "    pc_mark: {pc}\n"
        "    get_order_list_group_ids: {groups}\n"
        "  payment:\n"
        "    add_no_credit_card: '{credit}'\n"
        "    paypay_queue_auto_process: false\n"
    ).format(id=site_id, name=name, url=url, pc=pc, groups=groups, credit=credit)
