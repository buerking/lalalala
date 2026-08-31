"""
加购成功后的后台回调：POST addedCartCallbackSimple
在「打开商品页、确认数量价格、加入购物车」成功后调用，便于后台记录。
"""

import json
import logging
import subprocess
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

from src.utils.api_sign import (
    is_sign_failure,
    iter_sign_modes,
    iter_sign_secrets,
    params_for_sign,
    remember_sign_strategy,
)
from src.utils.sign_generator import SignGenerator
from src.utils.retry import call_api_with_retries, is_transient_http_error

# 验签类失败不叠长间隔（会换密钥/模式）；其它业务短重试
ADDED_CART_BUSINESS_RETRY_WAITS = (0.0, 2.0, 5.0)
ADDED_CART_SIGN_RETRY_WAITS = (0.0,)


def _make_cb_log(config: Optional[Dict[str, Any]] = None) -> Callable[..., None]:
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
    if value is True:
        return True
    if value in (1, "1", "true", "True", "TRUE"):
        return True
    return False


def _post_with_curl(url: str, body: Dict[str, str], timeout: int = 30):
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
            return int(lines[-1]), "\n".join(lines[:-1]).strip()
        return 0, out or (result.stderr or "")
    except FileNotFoundError:
        raise RuntimeError("未找到 curl 命令")
    except subprocess.TimeoutExpired:
        raise RuntimeError("curl 请求超时")


def _parse_added_cart_response(
    status_code: int,
    response_text: str,
    log: Callable[..., None],
) -> Tuple[bool, bool, str]:
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
    # 验签失败：可换密钥，但对同一密钥不必长间隔重试
    retryable = (not success) and (not is_sign_failure(msg))
    return success, retryable, msg or ("Success=false" if not success else "")


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
    Returns:
        (ok, message)
    密钥：优先订单 Secret（getOrderListSimple），与文档约定一致；可回退全局 secret。
    """
    _ = use_curl
    log = _make_cb_log(config)
    api_config = config.get("order_api") or {}
    url = (api_config.get("added_cart_callback_url") or "").strip()
    secrets = iter_sign_secrets(order, api_config)
    pull = order.get("_pull_site") if isinstance(order.get("_pull_site"), dict) else {}
    pc_mark = (
        str(pull.get("pc_mark") or "").strip()
        or (api_config.get("pc_mark") or "").strip()
    )

    if not url:
        msg = "未配置 order_api.added_cart_callback_url"
        log("%s，跳过回调", msg)
        return False, msg
    if not secrets or not pc_mark:
        msg = "未配置 order_api.secret 或 pc_mark"
        log("%s，跳过回调", msg)
        return False, msg

    order_id = str(order.get("order_id") or "").strip()
    mark_str = "" if order.get("mark") is None else str(order.get("mark")).strip()
    goods_id = str(product.get("goods_id") or "").strip()
    goods_no = str(product.get("goods_no") or "").strip()
    goods_number = int(product.get("quantity") or 0)
    # StoreName：调用方传入的 shop_id（站点固定中文名，与市场/书店配置一致）
    store_name = str(product.get("shop_id") or "").strip() or "骏河屋"
    order_sec = str(order.get("secret") or "").strip()
    global_sec = str(api_config.get("secret") or "").strip()

    if not goods_id or not goods_no:
        msg = "addedCart 缺少 GoodsId/GoodsNo（须来自 getOrderListSimple List）"
        log("%s goods_id=%r goods_no=%r", msg, goods_id, goods_no)
        return False, msg

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

    modes = iter_sign_modes(order, api_config)
    last_msg: Optional[str] = None
    log("========== addedCartCallbackSimple 开始 ==========")
    log("URL=%s", url)
    log(
        "入参快照 OrderId=%s GoodsId=%s GoodsNo=%s GoodsNumber=%s "
        "Mark=%s StoreName=%s PcMark=%s IsLack=%s IsLimit=%s IsNewOld=0",
        order_id,
        goods_id,
        goods_no,
        goods_number,
        mark_str,
        store_name,
        pc_mark,
        is_lack,
        is_limit,
    )
    log(
        "凭证 order.mark_len=%s order.secret_len=%s "
        "config.secret_len=%s pull_pc_mark=%s config.pc_mark=%s",
        len(mark_str),
        len(order_sec),
        len(global_sec),
        str(pull.get("pc_mark") or ""),
        str(api_config.get("pc_mark") or ""),
    )
    log(
        "product 原始字段 goods_id=%r goods_no=%r shop_id=%r quantity=%r "
        "(GoodsNo 须等于 getOrderListSimple List.GoodsNo)",
        product.get("goods_id"),
        product.get("goods_no"),
        product.get("shop_id"),
        product.get("quantity"),
    )
    log(
        "密钥优先序=%s 签名模式=%s sign_secret_prefer=%s",
        [x[0] for x in secrets],
        modes,
        str(api_config.get("sign_secret_prefer") or "order"),
    )
    log(
        "待签 base(JSON)=%s",
        json.dumps(base, ensure_ascii=False, sort_keys=True),
    )

    for sec_label, secret in secrets:
        for mode in modes:
            sign_src = params_for_sign(base, mode)
            omitted = sorted(set(base.keys()) - set(sign_src.keys()))
            gen = SignGenerator(secret)
            sign = gen.generate_sign(sign_src)
            concat_dbg = gen.debug_concat_string(sign_src, redact_secret=True)
            params = dict(base)
            params["Sign"] = sign
            log("---------- 尝试验签 secret=%s mode=%s ----------", sec_label, mode)
            log("参与签名字段(sorted)=%s", sorted(sign_src.keys()))
            if omitted:
                log("omit_empty 已排除空字段=%s", omitted)
            log(
                "参与签名参数(JSON)=%s",
                json.dumps(sign_src, ensure_ascii=False, sort_keys=True),
            )
            log("concatenatedString(密钥已脱敏)=%s", concat_dbg)
            log("GoodsNo(参与签名)=%r Sign=%s secret_len=%s", goods_no, sign, len(secret))
            log(
                "请求参数(form-data 完整)=%s",
                " ".join("%s=%s" % (k, v) for k, v in params.items()),
            )

            def _once(attempt_no: int, _params=params):
                nonlocal last_msg
                log("HTTP POST 第 %s 次", attempt_no)
                try:
                    status_code, response_text = _post_with_curl(url, _params, timeout)
                    log("响应状态码: %s", status_code)
                    body = response_text or ""
                    log("响应 body 全文: %s", body if len(body) <= 2000 else body[:2000] + "...(截断)")
                except Exception as e:
                    log("请求异常: %s %s", type(e).__name__, str(e))
                    last_msg = str(e)
                    return False, is_transient_http_error(0, str(e)), False

                ok, retryable, msg = _parse_added_cart_response(
                    status_code, response_text, log
                )
                last_msg = msg
                if (not ok) and is_sign_failure(str(msg or "")):
                    log(
                        "验签失败对照: GoodsNo=%r Mark=%r PcMark=%r "
                        "StoreName=%r secret=%s mode=%s Sign=%s",
                        _params.get("GoodsNo"),
                        _params.get("Mark"),
                        _params.get("PcMark"),
                        _params.get("StoreName"),
                        sec_label,
                        mode,
                        _params.get("Sign"),
                    )
                return ok, retryable, ok

            # 验签失败很快换下一密钥；非验签才短重试
            waits = ADDED_CART_SIGN_RETRY_WAITS
            ok = bool(call_api_with_retries("加购回调", _once, waits=waits))
            if ok:
                remember_sign_strategy(order, secret=secret, mode=mode)
                log("成功 secret=%s mode=%s", sec_label, mode)
                log("========== addedCartCallbackSimple 结束 ok ==========")
                return True, last_msg or ""

            tip = str(last_msg or "")
            if is_sign_failure(tip):
                log("本轮验签失败，换下一密钥/模式 Message=%s", tip)
                continue
            # 非验签：同一密钥再短重试一轮（含 2s/5s）
            ok = bool(
                call_api_with_retries(
                    "加购回调",
                    _once,
                    waits=ADDED_CART_BUSINESS_RETRY_WAITS,
                )
            )
            if ok:
                remember_sign_strategy(order, secret=secret, mode=mode)
                log("成功 secret=%s mode=%s", sec_label, mode)
                log("========== addedCartCallbackSimple 结束 ok ==========")
                return True, last_msg or ""
            tip = str(last_msg or "")
            if any(k in tip for k in ("只支持加入", "不符合自动下单", "已加入", "重复")):
                break

    if last_msg:
        log("最终失败 Message: %s", last_msg)
    log(
        "========== addedCartCallbackSimple 结束失败 GoodsNo=%s Message=%s ==========",
        goods_no,
        last_msg or "",
    )
    return False, last_msg or "addedCartCallbackSimple 失败"
