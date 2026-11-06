"""
代购订单自定义日志：POST addOrderLogSimple
用户端和后台都可见。本模块只封装接口，调用点由业务另行接入。
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

from src.utils.retry import call_api_with_retries, is_transient_http_error
from src.utils.sign_generator import SignGenerator

CONTENT_MAX_LEN = 500
DEFAULT_ADD_ORDER_LOG_URL = (
    "https://edi.jpgoodbuy.com/service.php?func=addOrderLogSimple"
)
# 自定义日志不是下单关键路径，网络失败只做短重试，避免卡住整单
ADD_ORDER_LOG_RETRY_WAITS = (0.0, 2.0, 5.0)


def _make_log(config: Optional[Dict[str, Any]] = None) -> Callable[..., None]:
    ns = ""
    if isinstance(config, dict):
        ns = str(config.get("_log_namespace") or "").strip()
    lg = logging.getLogger("site.%s" % ns if ns else "site.add_order_log")

    def _log(msg: str, *args) -> None:
        try:
            lg.info("[订单日志] " + msg, *args)
        except Exception:
            pass
        try:
            if args:
                print("[订单日志]", msg % args)
            else:
                print("[订单日志]", msg)
        except Exception:
            print("[订单日志]", msg)

    return _log


def _post_with_curl(url: str, body: Dict[str, str], timeout: int = 30) -> Tuple[int, str]:
    form_str = urlencode(body)
    cmd = [
        "curl", "-s", "-w", "\n%{http_code}",
        "-X", "POST",
        "--data", form_str,
        "--connect-timeout", "10",
        "--max-time", str(timeout),
        url,
    ]
    try:
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
            code = int(lines[-1])
            body_text = "\n".join(lines[:-1]).strip()
        else:
            code = 0
            body_text = out or (result.stderr or "")
        return code, body_text
    except FileNotFoundError:
        raise RuntimeError("未找到 curl 命令")
    except subprocess.TimeoutExpired:
        raise RuntimeError("curl 请求超时")


def add_order_log_url(api_config: Dict[str, Any]) -> str:
    url = str((api_config or {}).get("add_order_log_simple_url") or "").strip()
    if url:
        return url
    base = str((api_config or {}).get("get_order_list_simple_url") or "").strip()
    if "getOrderListSimple" in base:
        return base.replace("getOrderListSimple", "addOrderLogSimple")
    add_url = str((api_config or {}).get("add_no_callback_url") or "").strip()
    if "addNoCallbackSimple" in add_url:
        return add_url.replace("addNoCallbackSimple", "addOrderLogSimple")
    added_url = str((api_config or {}).get("added_cart_callback_url") or "").strip()
    if "addedCartCallbackSimple" in added_url:
        return added_url.replace("addedCartCallbackSimple", "addOrderLogSimple")
    return DEFAULT_ADD_ORDER_LOG_URL


def clip_order_log_content(content: str, max_len: int = CONTENT_MAX_LEN) -> str:
    text = str(content or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len]


def send_add_order_log_simple(
    order: Dict[str, Any],
    content: str,
    config: Dict[str, Any],
    use_curl: bool = True,
    timeout: int = 30,
) -> Tuple[bool, str, str]:
    """
    调用 addOrderLogSimple，给代购订单追加用户可见的自定义日志。

    Args:
        order: 订单字典（需含 order_id；签名用 secret / PcMark）
        content: 日志正文，必填，超过 500 字会截断
        config: 全局/站点合并后的 config
        use_curl: 使用 curl 发送（建议 True）
    Returns:
        (ok, error_message, raw_response_body)
    """
    from src.utils.api_sign import pick_sign_secret
    from src.utils.dev_test import skip_side_effects

    _log = _make_log(config)

    if skip_side_effects(config):
        return True, "", "dev_test_skip"

    api_config = config.get("order_api") or {}
    url = add_order_log_url(api_config)
    secret = pick_sign_secret(order, api_config)
    pull = order.get("_pull_site") if isinstance(order.get("_pull_site"), dict) else {}
    pc_mark = (
        str(pull.get("pc_mark") or "").strip()
        or str((api_config.get("pc_mark") or "")).strip()
    )
    if not url or not secret or not pc_mark:
        return False, "未配置 addOrderLogSimple URL / secret / pc_mark", ""

    order_id = str((order or {}).get("order_id") or "").strip()
    if not order_id:
        return False, "订单缺少 OrderId", ""

    content_str = clip_order_log_content(content)
    if not content_str:
        return False, "Content 为空", ""
    raw_content = str(content or "").strip()
    if len(raw_content) > CONTENT_MAX_LEN:
        _log(
            "Content 超长已截断 order=%s len=%s max=%s",
            order_id,
            len(raw_content),
            CONTENT_MAX_LEN,
        )

    params = {
        "OrderId": order_id,
        "PcMark": pc_mark,
        "Content": content_str,
    }
    sign_gen = SignGenerator(secret)
    params["Sign"] = sign_gen.generate_sign(params)

    _log("========== addOrderLogSimple 开始 ==========")
    _log("URL=%s", url)
    _log(
        "参数 OrderId=%s PcMark=%s Content=%s Sign=%s",
        order_id,
        pc_mark,
        content_str,
        params["Sign"],
    )

    def _once(attempt_no: int):
        _ = attempt_no
        try:
            if use_curl:
                code, body_text = _post_with_curl(url, params, timeout=timeout)
            else:
                import requests

                resp = requests.post(url, data=params, timeout=timeout)
                code = resp.status_code
                body_text = resp.text or ""
        except Exception as e:
            err = "请求异常: %s" % e
            return False, is_transient_http_error(0, err), (False, err, "")

        _log("响应状态码: %s", code)
        _log("响应 body 全文: %s", (body_text or "")[:800])

        if code != 200:
            err = "HTTP %s" % code
            _log("失败 %s", err)
            return False, is_transient_http_error(code, err), (False, err, body_text or "")

        try:
            data = json.loads(body_text) if body_text.strip() else {}
        except Exception:
            err = "响应非 JSON"
            _log("失败 %s body=%s", err, (body_text or "")[:300])
            return False, True, (False, err, body_text or "")

        if data.get("Success") is not True:
            err = "Success=false: %s" % (data.get("Message") or "")
            _log(
                "失败 %s ErrorCode=%s Data=%s",
                err,
                data.get("ErrorCode"),
                data.get("Data"),
            )
            return False, False, (False, err, body_text or "")
        if data.get("Data") is not True:
            err = "Data=false: %s" % (data.get("Message") or "")
            _log(
                "失败 %s ErrorCode=%s Success=%s",
                err,
                data.get("ErrorCode"),
                data.get("Success"),
            )
            return False, False, (False, err, body_text or "")
        _log("成功 Success=true Data=true ErrorCode=%s", data.get("ErrorCode"))
        return True, False, (True, "", body_text or "")

    result = call_api_with_retries(
        "订单日志",
        _once,
        waits=ADD_ORDER_LOG_RETRY_WAITS,
    )
    if isinstance(result, tuple) and len(result) == 3:
        ok, err, raw = result
        _log(
            "========== addOrderLogSimple 结束 ok=%s err=%s ==========",
            ok,
            err or "",
        )
        return ok, err, raw  # type: ignore[return-value]
    _log("========== addOrderLogSimple 结束 异常 %s ==========", result)
    return False, "请求异常: %s" % result, ""
