"""
下单成功后的回调：POST addNoCallbackSimple
"""

import json
import logging
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from src.utils.sign_generator import SignGenerator
from src.utils.retry import call_api_with_retries, is_transient_http_error


def _make_cb_log(config: Optional[Dict[str, Any]] = None) -> Callable[..., None]:
    ns = ""
    if isinstance(config, dict):
        ns = str(config.get("_log_namespace") or "").strip()
    lg = logging.getLogger("site.%s" % ns if ns else "site.add_no")

    def _log(msg: str, *args) -> None:
        try:
            lg.info("[完成回调] " + msg, *args)
        except Exception:
            pass
        try:
            if args:
                print("[完成回调]", msg % args)
            else:
                print("[完成回调]", msg)
        except Exception:
            print("[完成回调]", msg)

    return _log


def format_add_no_feishu_extra(
    store: str,
    order_id: str,
    purchase_no: str,
    err: str = "",
    raw: str = "",
) -> str:
    """飞书购买后文案：注文番号 + 订单ID + 后端原文。"""
    return (
        "%s：页面已下单成功，但 addNoCallbackSimple 未更新代购订单状态，请人工核对。"
        "订单ID=%s 注文番号=%s 后端=%s body=%s"
        % (
            store or "站点",
            str(order_id or "").strip() or "—",
            str(purchase_no or "").strip() or "—",
            (err or "").strip() or "—",
            (raw or "").strip()[:300] or "—",
        )
    )


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
    from src.utils.dev_test import skip_side_effects

    _log = _make_cb_log(config)

    if skip_side_effects(config):
        return True, "", "dev_test_skip"

    api_config = config.get("order_api") or {}
    url = (api_config.get("add_no_callback_url") or "").strip()
    from src.utils.api_sign import pick_sign_secret

    # 与加购一致：优先全局/已验证密钥
    secret = pick_sign_secret(order, api_config)
    # 市场→书店转交：优先订单上锁定的拉单站 PcMark
    pull = order.get("_pull_site") if isinstance(order.get("_pull_site"), dict) else {}
    pc_mark = (
        str(pull.get("pc_mark") or "").strip()
        or (api_config.get("pc_mark") or "").strip()
    )
    if not url or not secret or not pc_mark:
        return False, "未配置 add_no_callback_url / secret / pc_mark", ""

    order_id = str(order.get("order_id") or "").strip()
    mark_raw = order.get("mark")
    mark_str = "" if mark_raw is None else str(mark_raw)

    # YAML 纯数字会被解析成 int；签名与回传一律当字符串
    credit_card = str(credit_card or "").strip()
    # 转交锁定的 CreditCard 优先于入参（防止误传书店站默认）
    credit_locked = str(pull.get("credit_card") or "").strip()
    if credit_locked:
        credit_card = credit_locked

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

    _log("========== addNoCallbackSimple 开始 ==========")
    _log("URL=%s", url)
    _log(
        "参数 OrderId=%s CreditCard=%s PcMark=%s Mark_len=%s PurchaseNos=%s Sign=%s",
        order_id,
        credit_card,
        pc_mark,
        len(mark_str),
        purchase_json,
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

    result = call_api_with_retries("完成回调", _once)
    if isinstance(result, tuple) and len(result) == 3:
        ok, err, raw = result
        _log("========== addNoCallbackSimple 结束 ok=%s err=%s ==========", ok, err or "")
        return ok, err, raw  # type: ignore[return-value]
    _log("========== addNoCallbackSimple 结束 异常 %s ==========", result)
    return False, "请求异常: %s" % result, ""
