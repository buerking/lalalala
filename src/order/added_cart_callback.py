"""
加购成功后的后台回调：POST addedCartCallbackSimple
在「打开商品页、确认数量价格、加入购物车」成功后调用，便于后台记录。
"""

import json
import logging
import subprocess
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

from src.utils.sign_generator import SignGenerator
from src.utils.retry import call_api_with_retries, is_transient_http_error

# 加购回调：网络失败用长间隔；业务 Success=false 用短间隔再试几次（后端偶发）
ADDED_CART_BUSINESS_RETRY_WAITS = (0.0, 2.0, 5.0, 10.0)


def _make_cb_log(config: Optional[Dict[str, Any]] = None) -> Callable[..., None]:
    """写入当前站点日志文件（与 site.rakuten_books 等同文件）+ 控制台。"""
    ns = ""
    if isinstance(config, dict):
        ns = str(config.get("_log_namespace") or "").strip()
    lg = logging.getLogger(f"site.{ns}" if ns else "site.added_cart")

    def _log(msg: str, *args) -> None:
        try:
            lg.info("[加购回调] " + msg, *args)
        except Exception:
            pass
        try:
            if args:
                print("[加购回调]", msg % args)
            else:
                print("[加购回调]", msg)
        except Exception:
            print("[加购回调]", msg)

    return _log


def _is_api_success(value: Any) -> bool:
    """后端偶发返回字符串 true/1，不能只用 `is True`。"""
    if value is True:
        return True
    if value in (1, "1", "true", "True", "TRUE"):
        return True
    return False


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


def _params_for_sign(params: Dict[str, str], *, omit_empty: bool) -> Dict[str, str]:
    if not omit_empty:
        return dict(params)
    return {k: v for k, v in params.items() if v is not None and str(v) != ""}


def _parse_added_cart_response(
    status_code: int,
    response_text: str,
    log: Callable[..., None],
) -> Tuple[bool, bool, str]:
    """
    Returns:
        (ok, retryable, message)
    """
    if status_code != 200:
        log("非 200，视为失败")
        return (
            False,
            is_transient_http_error(status_code, "HTTP %s" % status_code),
            "HTTP %s" % status_code,
        )

    try:
        data = json.loads(response_text) if (response_text or "").strip() else {}
    except Exception:
        log("响应非 JSON，视为失败")
        return False, True, "响应非 JSON"

    success = _is_api_success(data.get("Success"))
    msg = str(data.get("Message") or "")
    log(
        "Success=%s Data=%s Message=%s ErrorCode=%s",
        data.get("Success"),
        data.get("Data"),
        data.get("Message"),
        data.get("ErrorCode"),
    )
    return success, (not success), msg or ("Success=false" if not success else "")


def send_added_cart_callback(
    order: Dict[str, Any],
    product: Dict[str, Any],
    config: Dict[str, Any],
    is_lack: int = 0,
    is_limit: int = 0,
    use_curl: bool = True,
    timeout: int = 30,
) -> Tuple[bool, str]:
    """
    加购成功后向后台发送 POST addedCartCallbackSimple。

    Returns:
        (ok, message)：ok 表示 200 且 Success 为真；message 为接口 Message 或失败原因。
        签名先 full，失败后再试 omit_empty（空字段不参与验签，与 getOrderSimple 一致）。
    """
    _ = use_curl
    log = _make_cb_log(config)
    api_config = config.get("order_api") or {}
    url = (api_config.get("added_cart_callback_url") or "").strip()
    secret = str(order.get("secret") or api_config.get("secret") or "").strip()
    pull = order.get("_pull_site") if isinstance(order.get("_pull_site"), dict) else {}
    pc_mark = (
        str(pull.get("pc_mark") or "").strip()
        or (api_config.get("pc_mark") or "").strip()
    )

    if not url:
        msg = "未配置 order_api.added_cart_callback_url"
        log("%s，跳过回调", msg)
        return False, msg
    if not secret or not pc_mark:
        msg = "未配置 order_api.secret 或 pc_mark"
        log("%s，跳过回调", msg)
        return False, msg

    order_id = str(order.get("order_id") or "").strip()
    mark_raw = order.get("mark")
    mark_str = "" if mark_raw is None else str(mark_raw).strip()
    goods_id = str(product.get("goods_id") or "").strip()
    goods_no = str(product.get("goods_no") or "").strip()
    goods_number = int(product.get("quantity") or 0)
    store_name = str(product.get("shop_id") or "").strip() or "骏河屋"

    base = {
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

    last_msg: Optional[str] = None
    log(
        "OrderId=%s GoodsId=%s GoodsNo=%s StoreName=%s PcMark=%s Mark=%s qty=%s",
        order_id,
        goods_id,
        goods_no,
        store_name,
        pc_mark,
        mark_str,
        goods_number,
    )

    for omit_empty in (False, True):
        sign_params = _params_for_sign(base, omit_empty=omit_empty)
        sign = SignGenerator(secret).generate_sign(sign_params)
        params = dict(base)
        params["Sign"] = sign
        mode_name = "omit_empty" if omit_empty else "full"
        log("签名模式=%s Sign=%s", mode_name, sign)
        log("请求 URL: %s", url)
        log(
            "请求参数(form-data): %s",
            " ".join("%s=%s" % (k, v) for k, v in params.items()),
        )

        def _once(attempt_no: int, _params=params):
            nonlocal last_msg
            _ = attempt_no
            try:
                status_code, response_text = _post_with_curl(url, _params, timeout)
                log("响应状态码: %s", status_code)
                log("响应 body: %s", (response_text or "")[:500])
            except Exception as e:
                log("请求异常: %s %s", type(e).__name__, str(e))
                last_msg = str(e)
                return False, is_transient_http_error(0, str(e)), False

            ok, retryable, msg = _parse_added_cart_response(
                status_code, response_text, log
            )
            last_msg = msg
            return ok, retryable, ok

        # full 多试几次；omit_empty 仅短试（避免双模式叠满约 34s）
        waits = ADDED_CART_BUSINESS_RETRY_WAITS if not omit_empty else (0.0, 2.0)
        ok = bool(call_api_with_retries("加购回调", _once, waits=waits))
        if ok:
            return True, last_msg or ""

        tip = str(last_msg or "")
        # 明确业务拒绝不必换签名模式空耗
        if any(k in tip for k in ("只支持加入", "不符合自动下单", "已加入", "重复")):
            break

    if last_msg:
        log("最终失败 Message: %s", last_msg)
    return False, last_msg or "addedCartCallbackSimple 失败"
