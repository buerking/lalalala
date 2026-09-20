"""
售罄删单退款：POST cancelOrderSimple
雅虎闲置在订单开始前识别到 API status=SOLD 时调用，避免人工反查。
"""

import json
import subprocess
from typing import Any, Dict, Tuple
from urllib.parse import urlencode

from src.utils.sign_generator import SignGenerator
from src.utils.retry import call_api_with_retries, is_transient_http_error

DEFAULT_CANCEL_REASON = "整单商品均已售出"


def _post_with_curl(url: str, body: Dict[str, str], timeout: int = 30) -> Tuple[int, str]:
    """使用系统 curl 发送 POST（application/x-www-form-urlencoded）。返回 (status_code, response_text)。"""
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


def _cancel_order_url(api_config: Dict[str, Any]) -> str:
    url = (api_config.get("cancel_order_simple_url") or "").strip()
    if url:
        return url
    base = (api_config.get("get_order_list_simple_url") or "").strip()
    if "getOrderListSimple" in base:
        return base.replace("getOrderListSimple", "cancelOrderSimple")
    add_url = (api_config.get("add_no_callback_url") or "").strip()
    if "addNoCallbackSimple" in add_url:
        return add_url.replace("addNoCallbackSimple", "cancelOrderSimple")
    return "https://edi.jpgoodbuy.com/service.php?func=cancelOrderSimple"


def send_cancel_order_simple(
    order: Dict[str, Any],
    config: Dict[str, Any],
    reason: str = DEFAULT_CANCEL_REASON,
    use_curl: bool = True,
    timeout: int = 30,
) -> Tuple[bool, str, str]:
    """
    调用 cancelOrderSimple 删单退款。

    Args:
        order: 订单字典（需含 order_id）
        config: 全局/站点合并后的 config
        reason: 取消原因（默认「整单商品均已售出」）
        use_curl: 使用 curl 发送（建议 True，避免 SSL 问题）
    Returns:
        (ok, error_message, raw_response_body)
        成功判定：Success 与 Data 均为 true。
        网络类失败会按：立即重试 → 1 分钟 → 5 分钟 再试。
    """
    from src.utils.dev_test import skip_side_effects

    if skip_side_effects(config):
        return True, "", "dev_test_skip"

    api_config = config.get("order_api") or {}
    url = _cancel_order_url(api_config)
    from src.utils.api_sign import pick_sign_secret

    secret = pick_sign_secret(order, api_config)
    pull = order.get("_pull_site") if isinstance(order.get("_pull_site"), dict) else {}
    pc_mark = (
        str(pull.get("pc_mark") or "").strip()
        or (api_config.get("pc_mark") or "").strip()
    )
    if not url or not secret or not pc_mark:
        return False, "未配置 cancelOrderSimple URL / secret / pc_mark", ""

    order_id = str(order.get("order_id") or "").strip()
    if not order_id:
        return False, "订单缺少 OrderId", ""

    reason_str = (reason or DEFAULT_CANCEL_REASON).strip() or DEFAULT_CANCEL_REASON
    params = {
        "OrderId": order_id,
        "PcMark": pc_mark,
        "Reason": reason_str,
    }
    sign_gen = SignGenerator(secret)
    params["Sign"] = sign_gen.generate_sign(params)

    print("[删单退款] 请求 URL:", url)
    print("[删单退款] 参数: OrderId=%s PcMark=%s Reason=%s" % (order_id, pc_mark, reason_str))
    print("[删单退款] Sign:", params["Sign"])

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

        print("[删单退款] 响应状态码:", code)
        print("[删单退款] 响应 body 前 300 字符:", (body_text or "")[:300])

        if code != 200:
            err = "HTTP %s" % code
            return False, is_transient_http_error(code, err), (False, err, body_text or "")

        try:
            data = json.loads(body_text) if body_text.strip() else {}
        except Exception:
            err = "响应非 JSON"
            return False, True, (False, err, body_text or "")

        if data.get("Success") is not True:
            err = "Success=false: %s" % (data.get("Message") or "")
            return False, False, (False, err, body_text or "")
        if data.get("Data") is not True:
            err = "Data=false: %s" % (data.get("Message") or "")
            return False, False, (False, err, body_text or "")
        return True, False, (True, "", body_text or "")

    result = call_api_with_retries("删单退款", _once)
    if isinstance(result, tuple) and len(result) == 3:
        return result  # type: ignore[return-value]
    return False, "请求异常: %s" % result, ""
