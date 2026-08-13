"""
分单商品回调：POST updateGoodsNoCallback
按取引番号打开详情页后，将商品列表 + 截图 URL 提交保存。
同一订单若有多分单，每个取引番号调用一次。
"""

import json
import subprocess
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode

from src.utils.sign_generator import SignGenerator
from src.utils.retry import call_api_with_retries, is_transient_http_error


def _post_with_curl(url: str, body: Dict[str, str], timeout: int = 30) -> Tuple[int, str]:
    """使用系统 curl 发送 POST（form-urlencoded）。返回 (status_code, response_text)。"""
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


def send_update_goods_no_callback(
    order: Dict[str, Any],
    purchase_no: str,
    goods_no_list: List[Dict[str, Any]],
    screenshot_url: str,
    store_name: str,
    config: Dict[str, Any],
    use_curl: bool = True,
    timeout: int = 30,
) -> Tuple[bool, str]:
    """
    调用 updateGoodsNoCallback：提交单个分单的商家单号、商品列表、截图 URL。

    Args:
        order: 订单字典（order_id、mark）
        purchase_no: 骏河屋取引番号（如 S2603065490）
        goods_no_list: [{"no":"品番","price":2000,"num":1}, ...]
        screenshot_url: 该分单详情页截图上传后的 URL
        store_name: 店铺名（tenpo_cd 或 骏河屋）
        config: 全局 config
        use_curl: 是否用 curl 发送
    Returns:
        (ok, error_message)
        网络类失败会按：立即重试 → 1 分钟 → 5 分钟 再试。
    """
    api_config = config.get("order_api") or {}
    url = (api_config.get("update_goods_no_callback_url") or "").strip()
    # 订单专属签名：优先使用 getOrderListSimple 返回的 order.secret
    secret = str(order.get("secret") or api_config.get("secret") or "").strip()
    pc_mark = (api_config.get("pc_mark") or "").strip()
    if not url or not secret or not pc_mark:
        return False, "未配置 update_goods_no_callback_url / secret / pc_mark"

    order_id = str(order.get("order_id") or "").strip()
    mark_raw = order.get("mark")
    mark_str = "" if mark_raw is None else str(mark_raw)

    goods_no_json = json.dumps(goods_no_list, separators=(",", ":"), ensure_ascii=False)
    screen_shot_urls_json = json.dumps([screenshot_url] if screenshot_url else [], ensure_ascii=False)

    params = {
        "PcMark": pc_mark,
        "OrderId": order_id,
        "GoodsNoList": goods_no_json,
        "PurchaseNo": purchase_no,
        "GrabNo": purchase_no,
        "Mark": mark_str,
        "StoreName": store_name or "骏河屋",
        "ScreenShotUrls": screen_shot_urls_json,
        "NeedNormal": "1",
    }
    sign_gen = SignGenerator(secret)
    params["Sign"] = sign_gen.generate_sign(params)

    print("[分单回调] updateGoodsNoCallback URL:", url)
    print("[分单回调] PurchaseNo=%s OrderId=%s StoreName=%s GoodsNoList 条数=%s" % (
        purchase_no, order_id, store_name, len(goods_no_list)))
    print("[分单回调] Sign:", params["Sign"])

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
            return False, is_transient_http_error(0, err), (False, err)

        print("[分单回调] 响应状态码:", code)
        print("[分单回调] 响应 body 前 300 字符:", (body_text or "")[:300])

        if code != 200:
            err = "HTTP %s" % code
            return False, is_transient_http_error(code, err), (False, err)

        try:
            data = json.loads(body_text) if body_text.strip() else {}
        except Exception:
            return False, True, (False, "响应非 JSON")

        # Success 必须为 True；Data 只在显式 False 时才视为失败，其余（True / [] / {} / null）均视为成功，
        # 以兼容返回 Data: [] 的情况。
        if data.get("Success") is not True:
            err = "Success=false: %s" % (data.get("Message") or "")
            return False, False, (False, err)

        data_field = data.get("Data", True)
        if isinstance(data_field, bool) and data_field is False:
            err = "Data=false: %s" % (data.get("Message") or "")
            return False, False, (False, err)

        return True, False, (True, "")

    result = call_api_with_retries("分单回调", _once)
    if isinstance(result, tuple) and len(result) == 2:
        return result  # type: ignore[return-value]
    return False, "请求异常: %s" % result
