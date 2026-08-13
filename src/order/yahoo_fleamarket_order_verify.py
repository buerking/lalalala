# -*- coding: utf-8 -*-
"""
雅虎闲置：支付后未进入完成页时的二次核验（伪 3DS / 页面卡住）。

通过商品 ID 拼接订单详情 URL 重新打开页面，若与「已成交」页面特征一致，则视为拍下成功，
供主流程继续走 addNo / 截图 / updateGoods 等逻辑，避免主流程臃肿。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from selenium.webdriver.common.by import By

from src.browser.browser_manager import BrowserManager

# item_id -> (api_json_or_None, err_or_None)
ItemApiCheck = Callable[[str], Tuple[Optional[dict], Optional[str]]]


def build_order_detail_candidate_urls(
    item_id: str, y_cfg: Dict[str, Any], current_url: str = ""
) -> List[str]:
    """
    按配置或当前域名生成待尝试的「订单/支付详情」URL 列表（同一 item 可能走 sec 主域）。
    """
    raw = y_cfg.get("order_detail_url_templates")
    if isinstance(raw, list) and raw:
        out: List[str] = []
        for t in raw:
            s = str(t).strip()
            if not s:
                continue
            try:
                out.append(s.format(item_id=item_id))
            except Exception:
                continue
        if out:
            return out

    cur = (current_url or "").lower()
    if "paypayfleamarket-sec" in cur:
        hosts = [
            "https://paypayfleamarket-sec.yahoo.co.jp",
            "https://paypayfleamarket.yahoo.co.jp",
        ]
    else:
        hosts = [
            "https://paypayfleamarket.yahoo.co.jp",
            "https://paypayfleamarket-sec.yahoo.co.jp",
        ]
    return ["%s/item/%s/order" % (h, item_id) for h in hosts]


def page_suggests_incomplete_payment_or_3ds(driver) -> bool:
    """页面仍像在未完成认证/支付流程，不应判为已成单。"""
    try:
        src = driver.page_source or ""
    except Exception:
        return False
    needles = (
        "3Dセキュア",
        "本人認証サービス",
        "認証を完了してください",
        "決済を完了してください",
        "お支払い手続きを完了",
    )
    return any(n in src for n in needles)


def page_indicates_completed_purchase(
    driver, item_id: str, y_cfg: Dict[str, Any]
) -> bool:
    """
    判定当前页面是否表现为「购买/支付已完成」（完成页或订单详情上的已付状态）。

    与主流程原 _is_success_page 逻辑对齐，并补充 /item/<id>/order 上可能出现的已决済文案。
    """
    iid = (item_id or "").strip().lower()
    cur = (driver.current_url or "").lower()
    done_path = "/item/%s/order/done" % iid
    if iid and done_path in cur:
        return True

    try:
        body_text = driver.page_source or ""
    except Exception:
        body_text = ""

    extra_markers = y_cfg.get("order_detail_completed_markers")
    if isinstance(extra_markers, list):
        for m in extra_markers:
            s = str(m).strip()
            if s and s in body_text:
                return True

    if "購入が完了" in body_text:
        return True

    # 完成页常见区块（原逻辑）
    try:
        if driver.find_elements(
            By.XPATH, "//*[contains(text(),'お支払い金額（合計）')]"
        ):
            return True
        if driver.find_elements(
            By.XPATH, "//*[contains(text(),'支払い手続き完了のお知らせ')]"
        ):
            return True
    except Exception:
        pass

    # /order（非 done）上已支付、已截标等（伪 3DS 后可能停在此 URL）
    tail_markers = (
        "支払い済み",
        "決済が完了",
        "お支払いが完了",
        "購入が完了しました",
        "購入手続きが完了",
    )
    if any(m in body_text for m in tail_markers):
        if iid and ("/item/%s/order" % iid) in cur:
            return True
        if "/order/done" in cur:
            return True

    return False


def try_recover_after_success_page_timeout(
    driver,
    item_id: str,
    y_cfg: Dict[str, Any],
    logger: Any,
    item_api_check: Optional[ItemApiCheck] = None,
) -> Tuple[bool, str]:
    """
    等待完成页超时后：依次打开订单详情候选 URL，判断是否已实际拍下。

    可选：商品 API 返回 status=SOLD 时，在页面无明显「未决済/3DS」提示且 URL 落在
    /item/<id>/order 时，视为已成交（减轻伪 3DS 后文案与完成页不一致的漏判）。

    Returns:
        (是否恢复为成功, 当前/最后尝试的详情页 URL)
    """
    if not y_cfg.get("stall_order_detail_verify", True):
        return False, ""

    sec = float(y_cfg.get("stall_verify_page_load_seconds", 3))
    last_url = ""
    candidates = build_order_detail_candidate_urls(
        item_id, y_cfg, driver.current_url or ""
    )

    for url in candidates:
        try:
            logger.info("雅虎闲置：完成页超时，二次核验打开订单详情 %s", url)
            BrowserManager.navigate_allow_timeout(driver, url, logger)
            time.sleep(sec)
        except Exception as e:
            logger.warning("二次核验打开 URL 失败 %s: %s", url, e)
            continue
        last_url = driver.current_url or url
        if page_suggests_incomplete_payment_or_3ds(driver):
            logger.info("二次核验：页面仍似未决済/3DS，尝试下一候选 URL")
            continue
        if page_indicates_completed_purchase(driver, item_id, y_cfg):
            logger.info("雅虎闲置：二次核验认为已成交，URL=%s", last_url)
            return True, last_url

    if item_api_check and y_cfg.get("stall_verify_item_api_fallback", True):
        try:
            data, _api_err = item_api_check(item_id)
        except Exception as e:
            logger.warning("二次核验：商品 API 检查异常 %s", e)
            data = None
        if data and str(data.get("status", "")).upper() == "SOLD":
            logger.info("二次核验：商品 API status=SOLD，打开订单详情页供后续截图")
            iid_lower = (item_id or "").strip().lower()
            for url in candidates:
                try:
                    BrowserManager.navigate_allow_timeout(driver, url, logger)
                    time.sleep(sec)
                except Exception as e:
                    logger.warning("二次核验(API)打开 URL 失败 %s: %s", url, e)
                    continue
                last_url = driver.current_url or url
                cur_l = (last_url or "").lower()
                if iid_lower and iid_lower not in cur_l:
                    continue
                if "/order" not in cur_l:
                    continue
                if page_suggests_incomplete_payment_or_3ds(driver):
                    continue
                logger.info("雅虎闲置：二次核验(API+URL) 视为已成交，URL=%s", last_url)
                return True, last_url

    return False, last_url
