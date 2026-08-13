"""
下单成功后的回调：POST addNoCallbackSimple
"""

import json
import subprocess
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode

from src.utils.sign_generator import SignGenerator
from src.utils.retry import call_api_with_retries, is_transient_http_error


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


def send_add_no_callback(
    order: Dict[str, Any],
    purchase_nos: List[Dict[str, str]],
    credit_card: str,
    config: Dict[str, Any],
    use_curl: bool = True,
    timeout: int = 30,
) -> Tuple[bool, str, str]:
    """
    下单成功回调 addNoCallbackSimple。

    Args:
        order: 订单字典（需含 order_id、mark）
        purchase_nos: [{"no": "...", "url": "..."}]（可能多分单）
        credit_card: paypay / paypal2167 / 货到付款
        config: 全局 config（order_api.add_no_callback_url/secret/pc_mark）
        use_curl: 使用 curl 发送（建议 True，避免 SSL 问题）
    Returns:
        (ok, error_message, raw_response_body)
        网络类失败会按：立即重试 → 1 分钟 → 5 分钟 再试。
    """
    api_config = config.get("order_api") or {}
    url = (api_config.get("add_no_callback_url") or "").strip()
    # 订单专属签名：优先使用 getOrderListSimple 返回的 order.secret
    secret = str(order.get("secret") or api_config.get("secret") or "").strip()
    pc_mark = (api_config.get("pc_mark") or "").strip()
    if not url or not secret or not pc_mark:
        return False, "未配置 add_no_callback_url / secret / pc_mark", ""

    order_id = str(order.get("order_id") or "").strip()
    mark_raw = order.get("mark")
    mark_str = "" if mark_raw is None else str(mark_raw)

    purchase_json = json.dumps(purchase_nos, separators=(",", ":"), ensure_ascii=False)
    params = {
        "OrderId": order_id,
        "PurchaseNos": purchase_json,
        "PcMark": pc_mark,
        "Mark": mark_str,
        "CreditCard": credit_card,
    }
    sign_gen = SignGenerator(secret)
    params["Sign"] = sign_gen.generate_sign(params)

    # 控制台打印，方便跟进确认
    print("[完成回调] 请求 URL:", url)
    print("[完成回调] 参数: OrderId=%s CreditCard=%s PurchaseNos=%s" % (order_id, credit_card, purchase_json))
    print("[完成回调] Sign:", params["Sign"])

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

        print("[完成回调] 响应状态码:", code)
        print("[完成回调] 响应 body 前 300 字符:", (body_text or "")[:300])

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

    result = call_api_with_retries("完成回调", _once)
    if isinstance(result, tuple) and len(result) == 3:
        return result  # type: ignore[return-value]
    return False, "请求异常: %s" % result, ""
