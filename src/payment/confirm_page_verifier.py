"""
订单确认页（cargo/orderconfirm）金额校验：
解析 合計金額 区域、全页截图并上传、调用 checkCartGoodsSimple。
若接口返回 Success=false 或 Data=false，应停止后续操作。
"""

import base64
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from urllib.parse import urlencode

from selenium.webdriver.common.by import By

from src.utils.sign_generator import SignGenerator
from src.utils.retry import call_api_with_retries, is_transient_http_error


# 浏览器窗口最大高度限制（像素），超出则只截到该高度
MAX_SCREENSHOT_HEIGHT = 12000


def _parse_yen_text(text: str) -> int:
    """从「359円」「1,039円」等文案解析出整数金额。"""
    if not text:
        return 0
    s = re.sub(r"[^\d]", "", str(text).strip())
    return int(s) if s else 0


def take_full_page_screenshot(driver, save_path: Optional[str] = None) -> str:
    """
    截取当前页面全部内容（包含不在视窗内的部分）。
    通过临时放大窗口高度再截图实现。

    Args:
        driver: Selenium WebDriver
        save_path: 保存路径；若为 None 则使用临时文件

    Returns:
        截图文件路径
    """
    if not save_path:
        fd, save_path = tempfile.mkstemp(suffix=".png")
        import os
        os.close(fd)

    try:
        total_height = driver.execute_script(
            "return Math.max("
            "document.body.scrollHeight, document.documentElement.scrollHeight,"
            "document.body.offsetHeight, document.documentElement.offsetHeight"
            ");"
        )
    except Exception:
        total_height = 2000
    height = min(int(total_height), MAX_SCREENSHOT_HEIGHT)
    width = 1920
    try:
        original_size = driver.get_window_size()
        driver.set_window_size(width, height)
        import time
        time.sleep(0.5)
        driver.save_screenshot(save_path)
        driver.set_window_size(
            original_size.get("width", width),
            original_size.get("height", 800),
        )
    except Exception:
        driver.set_window_size(width, 800)
        raise
    return save_path


def upload_screenshot_get_url(
    image_path: str,
    config: Dict[str, Any],
    folder: Optional[str] = None,
    use_requests: bool = True,
    use_curl: bool = False,
) -> Optional[str]:
    """
    将截图文件 base64 编码后 POST 到 common_upload.php，返回 Data 中的 URL。

    Args:
        image_path: 截图文件路径
        config: 全局 config，从中取 order_api.common_upload_url
        folder: 上传目录，默认 /surugayascreen
        use_requests: 是否用 requests（False 时用 curl，需自行处理 base64 表单）

    Returns:
        上传成功时返回 Data URL，失败返回 None
    """
    api_config = config.get("order_api") or {}
    url = (api_config.get("common_upload_url") or "").strip()
    if not url:
        print("[结算校验] 未配置 order_api.common_upload_url，无法上传截图")
        return None
    if folder is None:
        folder = (api_config.get("upload_folder") or "/surugayascreen").strip()

    path = Path(image_path)
    ext = path.suffix if path.suffix else ".png"
    name = f"{uuid.uuid4().hex}{ext}"

    with open(image_path, "rb") as f:
        raw = f.read()
    flow_b64 = base64.b64encode(raw).decode("ascii")

    body = {
        "folder": folder,
        "name": name,
        "flow": flow_b64,
    }

    print("[结算校验] 上传截图: folder=%s, name=%s, flow 长度=%d" % (folder, name, len(flow_b64)))

    # 与 getOrderSimple 一致：优先 curl 避免 Python requests 的 SSL UNEXPECTED_EOF 问题；body 过长写临时文件
    if not use_curl and use_requests:
        try:
            import requests
            resp = requests.post(url, data=body, timeout=60)
            print("[结算校验] 上传响应状态码:", resp.status_code)
            if resp.status_code != 200:
                print("[结算校验] 上传响应 body 前 300 字符:", (resp.text or "")[:300])
                return None
            data = resp.json()
        except Exception as e:
            print("[结算校验] 上传请求异常(将尝试 curl):", type(e).__name__, str(e))
            use_curl = True
    if use_curl:
        # curl: form-urlencoded；body 可能超长，写入临时文件再 curl -d @file
        form_str = urlencode(body)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".form", prefix="upload_")
        try:
            import os
            os.write(tmp_fd, form_str.encode("utf-8"))
            os.close(tmp_fd)
            data_arg = "@" + tmp_path.replace("\\", "/")
            cmd = [
                "curl", "-s", "-w", "\n%{http_code}",
                "-X", "POST",
                "--data", data_arg,
                "--connect-timeout", "30",
                "--max-time", "90",
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=100, encoding="utf-8")
            out = (result.stdout or "").strip()
            lines = out.split("\n")
            if lines and lines[-1].isdigit():
                code = int(lines[-1])
                body_text = "\n".join(lines[:-1]).strip()
            else:
                code = 0
                body_text = out or (result.stderr or "")
            print("[结算校验] 上传 curl 响应状态码:", code)
            if code != 200:
                print("[结算校验] 上传 curl 响应 body 前 300 字符:", (body_text or "")[:300])
                return None
            data = json.loads(body_text) if body_text else {}
        except Exception as e:
            print("[结算校验] 上传 curl 异常:", type(e).__name__, str(e))
            return None
        finally:
            try:
                import os
                os.unlink(tmp_path)
            except Exception:
                pass

    success = data.get("Success") is True
    data_url = data.get("Data") or ""
    print("[结算校验] 上传 Success=%s, Data=%s" % (success, data_url[:80] if data_url else ""))
    return data_url if success and data_url else None


def parse_confirm_summary(driver, config: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """
    从订单确认页解析 税込合計、送料、手数料、通信販売手数料、合計。
    支持两种页面结构：div#next_step_s（右侧）或 div.info_l 内含 h3「合計金額」（左侧）。

    Returns:
        {"goods_fee": 359, "operate_fee": 680, "total": 1039} 或 None（解析失败）
    """
    confirm_config = config.get("order_confirm_page") or {}
    container_sel = confirm_config.get("summary_container", "div#next_step_s")
    goods_fee = 0
    operate_fee = 0
    total = 0
    container = None
    try:
        container = driver.find_element(By.CSS_SELECTOR, container_sel)
    except Exception:
        # 部分页面为 div.info_l + h3「合計金額」结构，用 XPath 定位
        try:
            container = driver.find_element(
                By.XPATH,
                "//h3[contains(.,'合計金額')]/..",
            )
        except Exception:
            pass
    if not container:
        print("[结算校验] 解析合計金額失败: 未找到合計金額区域（已尝试 #next_step_s 与 合計金額 标题父节点）")
        return None
    try:
        dls = container.find_elements(By.CSS_SELECTOR, "dl")
        for dl in dls:
            dts = dl.find_elements(By.CSS_SELECTOR, "dt")
            dds = dl.find_elements(By.CSS_SELECTOR, "dd")
            if not dts or not dds:
                continue
            label = (dts[0].text or "").strip()
            value_el = dds[0]
            value_text = (value_el.text or "").strip()
            amount = _parse_yen_text(value_text)
            if "税込合計" in label:
                goods_fee = amount
            elif "送料" in label:
                operate_fee += amount
            elif label == "手数料":
                operate_fee += amount
            elif "通信販売手数料" in label:
                operate_fee += amount
            elif "合計" == label:
                total = amount
    except Exception as e:
        print("[结算校验] 解析合計金額失败:", type(e).__name__, str(e))
        return None

    print("[结算校验] 解析结果: 税込合計(GoodsFee)=%s, 送料+手数料+通信(OperateFee)=%s, 合計(Total)=%s" % (goods_fee, operate_fee, total))
    return {"goods_fee": goods_fee, "operate_fee": operate_fee, "total": total}


def build_goods_list(order: Dict[str, Any]) -> list:
    """从 order.products 构建 GoodsList JSON 数组：[{"No":"607217885","Num":1,"StoreName":"骏河屋","Price":500}]"""
    products = order.get("products") or []
    out = []
    for p in products:
        no = str(p.get("goods_no") or p.get("name") or "").strip()
        if not no:
            continue
        num = int(p.get("quantity") or 0)
        store_name = (str(p.get("shop_id") or "").strip()) or "骏河屋"
        price = int(float(p.get("price") or 0))
        out.append({"No": no, "Num": num, "StoreName": store_name, "Price": price})
    return out


def build_goods_list_from_confirm_page(driver, order: Dict[str, Any]) -> list:
    """
    从「ご注文内容の確認」页面 DOM 提取真实 GoodsList。

    页面结构通常为一到多张 table.order_list（可能分单），每张表商品行列：
    種類 / 商品名 / 管理番号 / 状態 / 数量 / 価格(税込)
    """
    # 尝试用订单 products 给 store_name 做映射；页面通常拿不到店铺名
    store_by_no = {}
    try:
        for p in (order.get("products") or []):
            no = str(p.get("goods_no") or p.get("name") or "").strip()
            if not no:
                continue
            store_name = (str(p.get("shop_id") or "").strip()) or "骏河屋"
            store_by_no[no] = store_name
    except Exception:
        store_by_no = {}

    out = []
    try:
        from selenium.webdriver.common.by import By

        tables = driver.find_elements(By.CSS_SELECTOR, "table.order_list")
        for table in tables:
            rows = table.find_elements(By.CSS_SELECTOR, "tr")
            for tr in rows:
                # 表头含 th
                try:
                    if tr.find_elements(By.TAG_NAME, "th"):
                        continue
                except Exception:
                    pass

                tds = tr.find_elements(By.TAG_NAME, "td")
                if len(tds) < 6:
                    continue

                # 合计/手数料/送料行多为 t_line04 或 colspan，且第一个 td 文案为「税込合計」「合計」「手数料」等
                first_text = (tds[0].text or "").strip()
                if any(k in first_text for k in ("税込合計", "合計", "手数料", "送料", "通信", "店舗の送料")):
                    continue

                no = (tds[2].text or "").strip()
                qty_text = (tds[4].text or "").strip()
                price_text = (tds[5].text or "").strip()
                if not no:
                    continue

                try:
                    num = int(re.sub(r"[^\d]", "", qty_text)) if qty_text else 0
                except Exception:
                    num = 0
                try:
                    price = int(re.sub(r"[^\d]", "", price_text)) if price_text else 0
                except Exception:
                    price = 0

                store_name = store_by_no.get(no) or "骏河屋"
                if num <= 0:
                    # 页面偶尔数量列为空，默认 1 以免后端认为缺失
                    num = 1
                out.append({"No": no, "Num": num, "StoreName": store_name, "Price": price})
    except Exception as e:
        print("[结算校验] 从确认页提取 GoodsList 失败:", type(e).__name__, str(e))
    return out


def check_cart_goods_simple(
    order: Dict[str, Any],
    total: int,
    goods_fee: int,
    operate_fee: int,
    screenshot_url: str,
    config: Dict[str, Any],
    goods_list_override: list = None,
    use_curl: bool = True,
    timeout: int = 30,
) -> Tuple[bool, str, str]:
    """
    调用 checkCartGoodsSimple 校验金额。若 Success=false 或 Data=false 应停止后续操作。

    Returns:
        (success, error_message, raw_response_body)
    """
    api_config = config.get("order_api") or {}
    url = (api_config.get("check_cart_goods_url") or "").strip()
    # 订单专属签名：优先使用 getOrderListSimple 返回的 order.secret
    secret = str(order.get("secret") or api_config.get("secret") or "").strip()
    pc_mark = (api_config.get("pc_mark") or "").strip()
    if not url or not secret or not pc_mark:
        return False, "未配置 check_cart_goods_url / secret / pc_mark", ""

    order_id = str(order.get("order_id") or "").strip()
    mark_raw = order.get("mark")
    mark_str = "" if mark_raw is None else str(mark_raw)
    goods_list = goods_list_override if goods_list_override is not None else build_goods_list(order)
    goods_list_json = json.dumps(goods_list, separators=(",", ":"), ensure_ascii=False)

    params = {
        "Mark": mark_str,
        "PcMark": pc_mark,
        "OrderId": order_id,
        "GoodsList": goods_list_json,
        "Total": str(total),
        "GoodsFee": str(goods_fee),
        "OperateFee": str(operate_fee),
        "ScreenShotUrl": screenshot_url or "",
    }
    sign_gen = SignGenerator(secret)
    # 部分后端在 ScreenShotUrl 为空时不参与验签，仅对非空参数做签名
    params_for_sign = {k: v for k, v in params.items() if (k != "ScreenShotUrl" or v)}
    params["Sign"] = sign_gen.generate_sign(params_for_sign)
    # 调试：打印参与签名的键和 Sign，便于与后端对照
    sorted_keys = sorted(params_for_sign.keys())
    print("[结算校验] 参与签名的参数键(排序):", sorted_keys)
    print("[结算校验] Sign:", params["Sign"])

    print("[结算校验] checkCartGoodsSimple 请求 URL:", url)
    print("[结算校验] 参数: OrderId=%s, Total=%s, GoodsFee=%s, OperateFee=%s, ScreenShotUrl=%s" % (
        order_id, total, goods_fee, operate_fee, (screenshot_url or "")[:60]
    ))
    print("[结算校验] GoodsList:", goods_list_json[:200])

    def _once(attempt_no: int):
        _ = attempt_no
        if use_curl:
            form_str = urlencode(params)
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
                    cmd, capture_output=True, text=True, timeout=timeout + 5, encoding="utf-8"
                )
                out = (result.stdout or "").strip()
                lines = out.split("\n")
                if lines and lines[-1].isdigit():
                    code = int(lines[-1])
                    body_text = "\n".join(lines[:-1]).strip()
                else:
                    code = 0
                    body_text = out or (result.stderr or "")
            except Exception as e:
                err = "checkCartGoodsSimple 请求异常: %s" % e
                return False, is_transient_http_error(0, err), (False, err, "")
        else:
            try:
                import requests
                resp = requests.post(url, data=params, timeout=timeout)
                code = resp.status_code
                body_text = resp.text or ""
            except Exception as e:
                err = "checkCartGoodsSimple 请求异常: %s" % e
                return False, is_transient_http_error(0, err), (False, err, "")

        print("[结算校验] checkCartGoodsSimple 响应状态码:", code)
        print("[结算校验] 响应 body 前 400 字符:", (body_text or "")[:400])

        if code != 200:
            err = "接口 HTTP %s" % code
            return False, is_transient_http_error(code, err), (False, err, body_text or "")

        try:
            data = json.loads(body_text) if body_text.strip() else {}
        except Exception:
            err = "响应非 JSON"
            return False, True, (False, err, body_text or "")

        success = data.get("Success") is True
        data_ok = data.get("Data") is True
        msg = data.get("Message") or ""

        if not success:
            err = "Success=false: %s" % msg
            return False, False, (False, err, body_text or "")
        if not data_ok:
            err = "Data=false: %s" % msg
            return False, False, (False, err, body_text or "")
        return True, False, (True, "", body_text or "")

    result = call_api_with_retries("结算校验", _once)
    if isinstance(result, tuple) and len(result) == 3:
        return result  # type: ignore[return-value]
    return False, "checkCartGoodsSimple 请求异常: %s" % result, ""
