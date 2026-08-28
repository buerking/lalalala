# -*- coding: utf-8 -*-
"""订单 Mark / Secret 刷新（getOrderSimple）。

错/过期 Mark 时：回调可能 Success=true，但后台订单状态不完结。
市场→书店转交后仍用拉单站 PcMark 刷新本单凭证。
"""

from __future__ import annotations

import json
from typing import Any, Dict

from src.utils.sign_generator import SignGenerator


def _post_curl(url: str, body: Dict[str, str], timeout: int = 20):
    import subprocess
    from urllib.parse import urlencode

    form_str = urlencode(body)
    cmd = [
        "curl",
        "-s",
        "-w",
        "\n%{http_code}",
        "-X",
        "POST",
        "--data",
        form_str,
        "--connect-timeout",
        "10",
        "--max-time",
        str(timeout),
        url,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout + 5,
        encoding="utf-8",
    )
    out = (result.stdout or "").strip()
    lines = out.split("\n")
    if lines and lines[-1].isdigit():
        return int(lines[-1]), "\n".join(lines[:-1]).strip()
    return 0, out or (result.stderr or "")


def refresh_order_mark(order: Dict[str, Any], config: Dict[str, Any]) -> None:
    """
    POST getOrderSimple 刷新本单 Mark/Secret。
    使用当前 config.order_api.pc_mark（拉单站身份，转交书店时仍为 rakuten）。
    失败抛 RuntimeError。
    """
    api = (config or {}).get("order_api") or {}
    detail_url = (api.get("get_order_detail_url") or "").strip()
    # 优先订单专属 secret，否则全局
    secret = str(
        (order or {}).get("secret") or api.get("secret") or ""
    ).strip()
    pc_mark = str(api.get("pc_mark") or "").strip()
    order_id = str((order or {}).get("order_id") or "").strip()
    if not detail_url or not secret or not order_id:
        raise RuntimeError("缺少 get_order_detail_url / secret / OrderId")
    if not pc_mark:
        raise RuntimeError("缺少 order_api.pc_mark（拉单站标识）")

    form = {"PcMark": pc_mark, "OrderId": order_id, "Mark": ""}
    form["Sign"] = SignGenerator(secret).generate_sign(
        {k: v for k, v in form.items() if v != ""}
    )
    code, body = _post_curl(detail_url, form, timeout=20)
    if code != 200 or not body:
        raise RuntimeError("HTTP %s" % code)
    data = json.loads(body)
    if data.get("Success") is not True:
        raise RuntimeError(str(data.get("Message") or "getOrderSimple 失败"))
    payload = data.get("Data")
    if not isinstance(payload, dict):
        raise RuntimeError("getOrderSimple Data 为空")

    new_mark = payload.get("Mark")
    new_secret = payload.get("Secret")
    if new_mark is not None and str(new_mark).strip():
        order["mark"] = str(new_mark).strip()
    if new_secret is not None and str(new_secret).strip():
        order["secret"] = str(new_secret).strip()
        order.pop("_sign_secret", None)
    elif new_mark is not None:
        order.pop("_sign_secret", None)
