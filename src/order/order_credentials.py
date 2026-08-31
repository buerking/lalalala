# -*- coding: utf-8 -*-
"""订单 Mark / Secret 刷新（getOrderSimple）。

错/过期 Mark 时：回调可能 Success=true，但后台订单状态不完结。
市场→书店转交后仍用拉单站 PcMark 刷新本单凭证。

验签：单订单回调优先 order.secret（拉单）；可用 sign_secret_prefer=global 覆盖。
"""

from __future__ import annotations

import json
from typing import Any, Dict

from src.utils.api_sign import (
    is_sign_failure,
    iter_sign_secrets,
    params_for_sign,
    remember_sign_strategy,
)
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
    secrets = iter_sign_secrets(order or {}, api)
    pc_mark = str(api.get("pc_mark") or "").strip()
    order_id = str((order or {}).get("order_id") or "").strip()
    if not detail_url or not order_id:
        raise RuntimeError("缺少 get_order_detail_url / OrderId")
    if not secrets:
        raise RuntimeError("缺少 order_api.secret / order.secret")
    if not pc_mark:
        raise RuntimeError("缺少 order_api.pc_mark（拉单站标识）")

    last_err = "getOrderSimple 失败"
    for sec_label, secret in secrets:
        form = {"PcMark": pc_mark, "OrderId": order_id, "Mark": ""}
        sign_src = params_for_sign(form, "omit_empty")
        form["Sign"] = SignGenerator(secret).generate_sign(sign_src)
        code, body = _post_curl(detail_url, form, timeout=20)
        if code != 200 or not body:
            last_err = "HTTP %s" % code
            continue
        try:
            data = json.loads(body)
        except Exception:
            last_err = "响应非 JSON"
            continue
        if data.get("Success") is not True:
            last_err = str(data.get("Message") or "getOrderSimple 失败")
            if is_sign_failure(last_err):
                continue
            raise RuntimeError(last_err)
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
        # 刷新接口本身用的密钥缓存，供后续 addedCart 优先
        remember_sign_strategy(order, secret=secret, mode="omit_empty")
        return

    raise RuntimeError("%s（已试密钥: %s）" % (last_err, [x[0] for x in secrets]))
