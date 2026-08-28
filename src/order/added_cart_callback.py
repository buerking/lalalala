"""
加购成功后的后台回调：POST addedCartCallbackSimple
在「打开商品页、确认数量价格、加入购物车」成功后调用，便于后台记录。
"""

import json
import logging
import subprocess
from typing import Dict, Any, Optional, Tuple
from urllib.parse import urlencode

from src.utils.sign_generator import SignGenerator
from src.utils.retry import call_api_with_retries, is_transient_http_error

# 加购回调：网络失败用长间隔；业务 Success=false 用短间隔再试几次（后端偶发）
ADDED_CART_BUSINESS_RETRY_WAITS = (0.0, 2.0, 5.0, 10.0)
_log = logging.getLogger("site.added_cart")


def _cb_log(msg: str, *args) -> None:
    try:
        _log.info(msg, *args)
    except Exception:
        pass
    try:
        if args:
            print("[加购回调]", msg % args)
        else:
            print("[加购回调]", msg)
    except Exception:
        print("[加购回调]", msg)


def _post_with_curl(url: str, body: Dict[str, str], timeout: int = 30):
    """使用系统 curl 发送 POST（application/x-www-form-urlencoded）。返回 (status_code, response_text)。"""
    pairs = [(k, "" if v is None else str(v)) for k, v in body.items()]
    form_str = urlencode(pairs, doseq=True, encoding="utf-8")
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


def _parse_added_cart_response(status_code: int, response_text: str) -> Tuple[bool, bool, str]:
    """
    Returns:
        (ok, retryable, message)
    """
    if status_code != 200:
        _cb_log("非 200，视为失败")
        return (
            False,
            is_transient_http_error(status_code, "HTTP %s" % status_code),
            "HTTP %s" % status_code,
        )

    try:
        data = json.loads(response_text) if (response_text or "").strip() else {}
    except Exception:
        _cb_log("响应非 JSON，视为失败")
        return False, True, "响应非 JSON"

    success = data.get("Success") is True
    msg = str(data.get("Message") or "")
    _cb_log(
        "Success=%s Data=%s Message=%s",
        data.get("Success"),
        data.get("Data"),
        data.get("Message"),
    )
    # Success=false：短间隔可重试（签名/瞬时业务）；最终仍失败再报工单
    return success, (not success), msg


def send_added_cart_callback(
    order: Dict[str, Any],
    product: Dict[str, Any],
    config: Dict[str, Any],
    is_lack: int = 0,
    is_limit: int = 0,
    use_curl: bool = True,
    timeout: int = 30,
) -> bool:
    """
    加购成功后向后台发送 POST addedCartCallbackSimple。

    Returns:
        True 表示接口返回 200 且 Success 为 true；否则 False。
        - 网络类失败：立即 / 1 分钟 / 5 分钟（call_api_with_retries 默认）
        - Success=false：短间隔再试若干次（见 ADDED_CART_BUSINESS_RETRY_WAITS）
    """
    api_config = config.get("order_api") or {}
    url = (api_config.get("added_cart_callback_url") or "").strip()
    secret = str(order.get("secret") or api_config.get("secret") or "").strip()
    pull = order.get("_pull_site") if isinstance(order.get("_pull_site"), dict) else {}
    pc_mark = (
        str(pull.get("pc_mark") or "").strip()
        or (api_config.get("pc_mark") or "").strip()
    )

    if not url:
        _cb_log("未配置 order_api.added_cart_callback_url，跳过回调")
        return False
    if not secret or not pc_mark:
        _cb_log("未配置 order_api.secret 或 pc_mark，跳过回调")
        return False

    order_id = str(order.get("order_id") or "").strip()
    mark_raw = order.get("mark")
    mark_str = "" if mark_raw is None else str(mark_raw)
    goods_id = str(product.get("goods_id") or "").strip()
    goods_no = str(product.get("goods_no") or "").strip()
    goods_number = int(product.get("quantity") or 0)
    store_name = str(product.get("shop_id") or "").strip() or "骏河屋"

    params = {
        "OrderId": order_id,
        "GoodsId": goods_id,
        "GoodsNo": goods_no,
        "IsLack": str(is_lack),
        "IsLimit": str(is_limit),
        "IsNewOld": "0",
        "Mark": mark_str,
        "GoodsNumber": str(goods_number),
        "StoreName": store_name,
        "PcMark": pc_mark,
    }
    sign_gen = SignGenerator(secret)
    sign = sign_gen.generate_sign(params)
    params["Sign"] = sign

    _cb_log("请求 URL: %s", url)
    _cb_log(
        "请求参数(form-data): %s",
        " ".join("%s=%s" % (k, v) for k, v in params.items()),
    )
    _cb_log("Sign: %s", sign)

    last_msg: Optional[str] = None

    def _once(attempt_no: int):
        nonlocal last_msg
        _ = attempt_no
        try:
            status_code, response_text = _post_with_curl(url, params, timeout)
            _cb_log("响应状态码: %s", status_code)
            _cb_log("响应 body: %s", (response_text or "")[:500])
        except Exception as e:
            _cb_log("请求异常: %s %s", type(e).__name__, str(e))
            last_msg = str(e)
            return False, is_transient_http_error(0, str(e)), False

        ok, retryable, msg = _parse_added_cart_response(status_code, response_text)
        last_msg = msg
        return ok, retryable, ok

    # 先按业务短重试；若仍失败且像网络问题，外层再走长间隔（此处合并为短重试优先）
    ok = bool(
        call_api_with_retries(
            "加购回调",
            _once,
            waits=ADDED_CART_BUSINESS_RETRY_WAITS,
        )
    )
    if not ok and last_msg:
        _cb_log("最终失败 Message: %s", last_msg)
    return ok