# -*- coding: utf-8 -*-
"""
雅虎闲置（paypayfleamarket）单商品下单：
无购物车，详情 → 下单页 → 「購入内容を確認する」→ 二次确认(#confirm_ok) → 完成页。
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.browser.browser_manager import BrowserManager
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.ticket_creator import TicketCreator
from src.order.add_no_callback import send_add_no_callback
from src.order.added_cart_callback import send_added_cart_callback
from src.order.update_goods_no_callback import send_update_goods_no_callback
from src.payment.confirm_page_verifier import (
    take_full_page_screenshot,
    upload_screenshot_get_url,
    check_cart_goods_simple,
)
from src.utils.logger import LoggerMixin
from src.order.yahoo_fleamarket_order_verify import (
    page_indicates_completed_purchase,
    try_recover_after_success_page_timeout,
)


_ITEM_PATH_RE = re.compile(r"/item/(?P<id>[^/?#]+)", re.IGNORECASE)
# 页面金额：17,300円 / 17300円；也匹配「クレジットカード：7,600円」
_YEN_AMOUNT_RE = re.compile(
    r"(?:クレジットカード[：:]\s*)?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*円"
)
_YEN_ONLY_RE = re.compile(r"^\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*円\s*$")


def extract_yahoo_item_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = _ITEM_PATH_RE.search(url)
    if m:
        return m.group("id")
    # 兜底：URL 末段
    parts = url.rstrip("/").split("/")
    if parts:
        last = parts[-1]
        if re.match(r"^[a-zA-Z0-9_-]+$", last):
            return last
    return None


class YahooFleaMarketOrderProcessor(LoggerMixin):
    def __init__(self, config: Dict[str, Any], browser_manager: BrowserManager):
        self.config = config
        self.browser_manager = browser_manager
        self.y_cfg = config.get("yahoo_fleamarket") or {}
        self.ticket_creator = TicketCreator(config)
        self.feishu_notifier = FeishuNotifier(config)

    @staticmethod
    def _parse_float_range(v: Any, default_min: float, default_max: float) -> Tuple[float, float]:
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                a = float(v[0])
                b = float(v[1])
                return (min(a, b), max(a, b))
            except Exception:
                pass
        return (default_min, default_max)

    def _random_pre_click_wait(self, action: str) -> None:
        """
        关键点击前随机等待，降低机械节奏特征。
        优先读 yahoo_fleamarket.pre_click_wait_seconds_range，回退 payment.pre_click_wait_seconds_range。
        """
        pay_cfg = self.config.get("payment") or {}
        rng = self.y_cfg.get("pre_click_wait_seconds_range")
        if rng is None:
            rng = pay_cfg.get("pre_click_wait_seconds_range")
        mn, mx = self._parse_float_range(rng, 0.7, 1.8)
        if mx <= 0:
            return
        sec = random.uniform(max(0.0, mn), max(0.0, mx))
        self.logger.info("关键点击前随机等待 %.2f 秒（%s）", sec, action)
        time.sleep(sec)

    def _store_name(self) -> str:
        return (self.y_cfg.get("store_name") or "雅虎闲置").strip()

    def _credit_card(self) -> str:
        return (self.config.get("payment") or {}).get("add_no_credit_card", "GMO2167")

    @staticmethod
    def _yen_text_to_int(raw: str) -> Optional[int]:
        s = (raw or "").replace(",", "").replace("，", "").strip()
        if not s.isdigit():
            return None
        try:
            v = int(s)
        except Exception:
            return None
        return v if v > 0 else None

    def _extract_page_price_yen(self, driver) -> Optional[int]:
        """
        从当前页可见文案解析成交金额（日元整数）。
        优先：支付行「クレジットカード：N円」；其次：仅含「N円」的短节点众数；
        再兜底：整页文案中出现最多的金额。不依赖易变的 sc-* class。
        """
        texts: List[str] = []
        try:
            for el in driver.find_elements(By.XPATH, "//*[contains(text(),'円')]"):
                try:
                    t = (el.text or "").strip()
                except Exception:
                    t = ""
                if t:
                    texts.append(t)
        except Exception:
            pass

        pay_hits: List[int] = []
        only_hits: List[int] = []
        all_hits: List[int] = []

        for t in texts:
            compact = t.replace("\u00a0", " ").replace(" ", "")
            for m in re.finditer(r"クレジットカード[：:]([0-9,]+)円", compact):
                v = self._yen_text_to_int(m.group(1))
                if v:
                    pay_hits.append(v)
            if _YEN_ONLY_RE.match(t) or _YEN_ONLY_RE.match(compact):
                m2 = _YEN_AMOUNT_RE.search(compact)
                if m2:
                    v = self._yen_text_to_int(m2.group(1))
                    if v:
                        only_hits.append(v)
            for m3 in _YEN_AMOUNT_RE.finditer(compact):
                v = self._yen_text_to_int(m3.group(1))
                if v:
                    all_hits.append(v)

        def _mode(vals: List[int]) -> Optional[int]:
            if not vals:
                return None
            # 众数；并列时取较大值（避免误抓到小额杂项）
            counts: Dict[int, int] = {}
            for v in vals:
                counts[v] = counts.get(v, 0) + 1
            best = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[0][0]
            return best

        for bucket in (pay_hits, only_hits, all_hits):
            picked = _mode(bucket)
            if picked:
                return picked

        # DOM 节点拿不到时，回退整页源码粗扫（仍用众数）
        try:
            src = driver.page_source or ""
        except Exception:
            src = ""
        src_hits: List[int] = []
        for m in _YEN_AMOUNT_RE.finditer(src.replace("\u00a0", "")):
            v = self._yen_text_to_int(m.group(1))
            if v and v < 100_000_000:  # 排除明显噪声
                src_hits.append(v)
        return _mode(src_hits)

    def _require_page_price(self, driver, stage: str) -> Tuple[Optional[int], str]:
        """
        在调用需传价的后端接口前，强制从当前页取真实价格。
        返回 (price_int, err_msg)；成功时 err_msg 为空。
        """
        price = self._extract_page_price_yen(driver)
        if price and price > 0:
            self.logger.info("雅虎闲置：页面真实价格 %s 円（阶段=%s）", price, stage)
            return price, ""
        return None, "无法从当前页面解析真实价格（阶段=%s）" % stage

    def _item_api_url(self, item_id: str) -> str:
        tpl = (
            self.y_cfg.get("item_api_template")
            or "https://paypayfleamarket.yahoo.co.jp/api/item/v2/items/{item_id}"
        )
        return tpl.format(item_id=item_id)

    def _item_api_headers(self, item_id: str) -> Dict[str, str]:
        """与页面请求接近的头：部分环境裸 requests 会被拒或握手异常，需 Referer/User-Agent。"""
        ref_tpl = (
            self.y_cfg.get("item_api_referer_template")
            or "https://paypayfleamarket.yahoo.co.jp/item/{item_id}"
        )
        referer = ref_tpl.format(item_id=item_id)
        ua = self.y_cfg.get("item_api_user_agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        return {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Referer": referer,
            "Origin": "https://paypayfleamarket.yahoo.co.jp",
        }

    def _fetch_item_via_curl(
        self,
        full_url: str,
        headers: Dict[str, str],
        timeout: int,
        verify_ssl: bool,
    ) -> Tuple[Optional[dict], Optional[str]]:
        cmd: List[str] = [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            "--connect-timeout",
            "10",
            "--max-time",
            str(timeout),
        ]
        if not verify_ssl:
            cmd.append("-k")
        for k, v in headers.items():
            cmd.extend(["-H", "%s: %s" % (k, v)])
        cmd.append(full_url)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                encoding="utf-8",
            )
        except FileNotFoundError:
            return None, "未找到 curl，无法拉取雅虎商品 API"
        except subprocess.TimeoutExpired:
            return None, "curl 请求超时"
        out = (result.stdout or "").strip()
        lines = out.split("\n")
        if not lines:
            return None, "curl 无输出"
        code_line = lines[-1].strip()
        body_text = "\n".join(lines[:-1]).strip()
        if not code_line.isdigit():
            return None, "curl 响应异常: %s" % (body_text[:200] or code_line)
        code = int(code_line)
        if code != 200:
            return None, "HTTP %s %s" % (code, (body_text or "")[:200])
        try:
            return (json.loads(body_text) if body_text else None), None
        except Exception:
            return None, "响应非 JSON: %s" % (body_text or "")[:200]

    def _fetch_item_via_requests(
        self,
        url: str,
        params: Dict[str, str],
        headers: Dict[str, str],
        timeout: int,
        verify: bool,
        use_tls12: bool,
    ) -> Tuple[Optional[dict], Optional[str]]:
        try:
            if use_tls12:
                from requests import Session

                from src.order.order_fetcher import TLS12Adapter

                sess = Session()
                sess.mount("https://", TLS12Adapter())
                r = sess.get(url, params=params, headers=headers, timeout=timeout, verify=verify)
            else:
                r = requests.get(url, params=params, headers=headers, timeout=timeout, verify=verify)
            if r.status_code != 200:
                return None, "HTTP %s" % r.status_code
            return r.json(), None
        except Exception as e:
            return None, str(e)

    def _fetch_item_status(self, item_id: str) -> Tuple[Optional[dict], Optional[str]]:
        url = self._item_api_url(item_id).strip()
        api = self.config.get("order_api") or {}
        timeout = int(api.get("timeout", 30))
        verify = bool(api.get("verify_ssl", True))
        use_tls12 = bool(api.get("use_tls12", True))
        headers = self._item_api_headers(item_id)
        params = {
            "needCrumb": "true",
            "t": str(int(time.time() * 1000)),
        }
        full_url = "%s%s%s" % (
            url,
            "&" if "?" in url else "?",
            urlencode(params),
        )

        prefer_curl = self.y_cfg.get("item_api_use_curl")
        if prefer_curl is None:
            prefer_curl = bool(api.get("use_curl_for_order_api", False))

        if prefer_curl:
            data, err = self._fetch_item_via_curl(full_url, headers, timeout, verify)
            if data is not None:
                self.logger.debug("雅虎商品API：curl 成功 id=%s", item_id)
                return data, None
            self.logger.warning("雅虎商品API：curl 失败 id=%s err=%s，改试 requests", item_id, err)

        data, err = self._fetch_item_via_requests(
            url, params, headers, timeout, verify, use_tls12
        )
        if data is not None:
            self.logger.debug("雅虎商品API：requests 成功 id=%s", item_id)
            return data, None
        self.logger.warning("雅虎商品API：requests 失败 id=%s err=%s", item_id, err)

        if not prefer_curl and self.y_cfg.get("item_api_fallback_curl", True):
            data2, err2 = self._fetch_item_via_curl(full_url, headers, timeout, verify)
            if data2 is not None:
                self.logger.info("雅虎商品API：requests 失败后 curl 成功 id=%s", item_id)
                return data2, None
            return None, err2 or err

        return None, err

    def _make_summary(
        self,
        order: Dict[str, Any],
        *,
        success: bool = False,
        failure_reason: str = "",
        payment_method: str = "yahoo_creditcard",
        check_cart_requested: bool = False,
        check_cart_response: str = "未请求",
        add_no_requested: bool = False,
        add_no_response: str = "未请求",
        update_errors: Optional[List[str]] = None,
        runner_pause_requested: bool = False,
    ) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "order_no": str(order.get("order_no") or order.get("order_id") or ""),
            "success": success,
            "payment_method": payment_method,
            "failure_reason": failure_reason,
            "check_cart_requested": check_cart_requested,
            "check_cart_response": check_cart_response,
            "add_no_requested": add_no_requested,
            "add_no_response": add_no_response,
            "update_errors": update_errors or [],
        }
        if runner_pause_requested:
            d["runner_pause_requested"] = True
        return d

    def _dismiss_ok_modal(self, driver, timeout: float = 5.0) -> None:
        """下单后可能出现的 React 弹窗（例：置き配说明），点 OK。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                btns = driver.find_elements(
                    By.XPATH,
                    "//div[@role='dialog']//button[.//div[normalize-space()='OK']]",
                )
                for b in btns:
                    if b.is_displayed():
                        self._random_pre_click_wait("置き配弹窗OK")
                        driver.execute_script("arguments[0].click();", b)
                        time.sleep(0.5)
                        return
            except Exception:
                pass
            time.sleep(0.3)

    def _click_confirm_purchase(self, driver, timeout: int = 25) -> None:
        xp = self.y_cfg.get("confirm_purchase_button_xpath") or (
            "//button[.//div[normalize-space()='購入内容を確認する']]"
        )
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xp))
        )
        self._random_pre_click_wait("購入内容を確認する")
        driver.execute_script("arguments[0].click();", el)
        time.sleep(float(self.y_cfg.get("wait_after_confirm_click_seconds", 2)))

    def _click_confirm_ok(self, driver, timeout: int = 25) -> None:
        """第二次确认弹窗：点击 <a id='confirm_ok'>OK</a>。"""
        css = (self.y_cfg.get("confirm_ok_selector") or "a#confirm_ok").strip()
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, css))
        )
        self._random_pre_click_wait("confirm_ok")
        driver.execute_script("arguments[0].click();", el)
        time.sleep(float(self.y_cfg.get("wait_after_confirm_ok_seconds", 2)))

    def _is_success_page(self, driver, item_id: str) -> bool:
        """成功判定逻辑见 yahoo_fleamarket_order_verify.page_indicates_completed_purchase。"""
        return page_indicates_completed_purchase(driver, item_id, self.y_cfg)

    def _wait_success_page(self, driver, item_id: str) -> Tuple[bool, str]:
        sec = int(self.y_cfg.get("post_confirm_wait_seconds", 120))
        try:
            WebDriverWait(driver, sec).until(lambda d: self._is_success_page(d, item_id))
            return True, driver.current_url
        except TimeoutException:
            return False, driver.current_url or ""

    def _detect_seller_restriction_modal(self, driver) -> str:
        """
        支付确认后出现「購入できません」且说明为出品者限制购买对象时，返回简短原因文案；否则返回空。
        与 3DS 无关，应跳过本单并继续后续调度。
        """
        try:
            dialogs = driver.find_elements(By.CSS_SELECTOR, "div[role='dialog']")
        except Exception:
            return ""
        for d in dialogs:
            try:
                if not d.is_displayed():
                    continue
            except Exception:
                continue
            try:
                h2_text = ""
                for h2 in d.find_elements(By.TAG_NAME, "h2"):
                    h2_text = (h2.text or "").strip()
                    if "購入できません" in h2_text:
                        break
                if "購入できません" not in h2_text:
                    continue
                body = ""
                for p in d.find_elements(By.TAG_NAME, "p"):
                    t = (p.text or "").strip()
                    if t:
                        body = t
                        break
                if "出品者" in body and "制限" in body:
                    return body or "出品者により購入可能ユーザーが制限されています"
            except Exception:
                continue
        try:
            src = driver.page_source or ""
        except Exception:
            src = ""
        if "購入できません" in src and "出品者" in src and "制限" in src:
            return "この商品は出品者により「購入」ができるユーザーが制限されています"
        return ""

    def _dismiss_seller_restriction_modal(self, driver) -> None:
        """关闭「購入できません」弹窗内的 OK。"""
        try:
            for el in driver.find_elements(
                By.XPATH,
                "//div[@role='dialog']//a[.//div[normalize-space()='OK']]",
            ):
                try:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(0.4)
                        return
                except Exception:
                    continue
        except Exception:
            pass

    def _detect_page_not_found(self, driver) -> bool:
        """
        最终确认后商品已被售出/删除时，常见跳转至 404：
        「ご指定のページが見つかりませんでした」
        """
        markers = self.y_cfg.get("page_not_found_texts") or [
            "ご指定のページが見つかりませんでした",
            "指定のページが見つかりませんでした",
            "ページが見つかりませんでした",
        ]
        try:
            src = driver.page_source or ""
        except Exception:
            src = ""
        if any(str(m).strip() and str(m) in src for m in markers):
            return True
        try:
            title = (driver.title or "").strip()
        except Exception:
            title = ""
        if "見つかりません" in title or "404" in title:
            return True
        try:
            url = (driver.current_url or "").lower()
        except Exception:
            url = ""
        if "/error" in url or "notfound" in url or "not_found" in url:
            # URL  alone 不够稳，需配合文案；已有文案则上面已返回
            pass
        return False

    def _wait_success_or_seller_restriction(
        self, driver, item_id: str
    ) -> Tuple[str, str]:
        """
        二次确认后：轮询直到成功页、卖家购限弹窗、商品页 404、或超时。

        Returns:
            ("success", current_url)
            ("seller_restricted", 原因摘要)
            ("page_not_found", 当前 URL)
            ("timeout", 当前 URL)
        """
        sec = int(self.y_cfg.get("post_confirm_wait_seconds", 120))
        poll = float(self.y_cfg.get("post_confirm_poll_seconds", 1.5))
        deadline = time.time() + max(5, sec)
        while time.time() < deadline:
            if self._detect_page_not_found(driver):
                return "page_not_found", driver.current_url or ""
            reason = self._detect_seller_restriction_modal(driver)
            if reason:
                self._dismiss_seller_restriction_modal(driver)
                return "seller_restricted", reason
            if self._is_success_page(driver, item_id):
                return "success", driver.current_url or ""
            time.sleep(max(0.3, poll))
        if self._detect_page_not_found(driver):
            return "page_not_found", driver.current_url or ""
        reason2 = self._detect_seller_restriction_modal(driver)
        if reason2:
            self._dismiss_seller_restriction_modal(driver)
            return "seller_restricted", reason2
        return "timeout", driver.current_url or ""

    def process_order(self, order: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        order_id = order.get("order_id", "未知")
        products: List[Dict[str, Any]] = order.get("products") or []
        self.logger.info("雅虎闲置：开始处理订单 %s，商品数 %s", order_id, len(products))

        if not products:
            return False, self._make_summary(order, failure_reason="订单无商品")

        if len(products) > 1:
            self.logger.warning("雅虎闲置当前按单商品处理，本单含 %s 个商品，仅处理第一个", len(products))

        product = products[0]
        product_url = (product.get("url") or "").strip()
        item_id = extract_yahoo_item_id(product_url)
        if not item_id:
            return False, self._make_summary(order, failure_reason="无法从商品 URL 解析 item id: %s" % product_url)

        # 优先 API 校验可买
        data, err = self._fetch_item_status(item_id)
        if data and str(data.get("status", "")).upper() == "SOLD":
            msg = "商品已售出（API status=SOLD） item=%s url=%s" % (item_id, product_url)
            self.logger.warning(msg)
            self._handle_order_issue(order, [msg], reason="雅虎闲置库存")
            return False, self._make_summary(order, failure_reason=msg)
        if err:
            self.logger.warning("雅虎闲置：商品 API 请求失败（将尝试页面兜底）: %s", err)

        driver = self.browser_manager.get_driver()
        use_curl = (self.config.get("order_api") or {}).get("use_curl_for_order_api", True)

        buy_wait = int(self.y_cfg.get("product_page_buy_button_wait_seconds", 45))
        self.logger.info("雅虎闲置：打开商品页（可不等待整页加载完成）%s", product_url)
        try:
            self.browser_manager.navigate(product_url)
        except Exception as e:
            self.logger.error("雅虎闲置：导航商品页异常 %s: %s", product_url, e)
            return False, self._make_summary(order, failure_reason="打开商品页失败: %s" % e)

        try:
            WebDriverWait(driver, buy_wait).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a#item_buy_button"))
            )
            time.sleep(float(self.y_cfg.get("wait_after_product_load_seconds", 2)))
            self.logger.info(
                "雅虎闲置：商品页已出现购买按钮，当前 URL: %s",
                driver.current_url or "",
            )
        except TimeoutException:
            msg = (
                "商品页在 %s 秒内未出现购买按钮 #item_buy_button，可能未登录/已售出/页面异常 item=%s"
                % (buy_wait, item_id)
            )
            self.logger.warning(msg)
            try:
                self._handle_order_issue(order, [msg], reason="雅虎闲置不可购")
            except Exception:
                pass
            return False, self._make_summary(order, failure_reason=msg)

        # 页面兜底：无购买按钮则视为不可买
        buy_links = driver.find_elements(By.CSS_SELECTOR, "a#item_buy_button")
        if not buy_links:
            msg = "页面上未找到购买按钮 #item_buy_button，可能已售出或不可购 item=%s" % item_id
            self.logger.warning(msg)
            try:
                self._handle_order_issue(order, [msg], reason="雅虎闲置不可购")
            except Exception:
                pass
            return False, self._make_summary(order, failure_reason=msg)

        clickable = None
        for a in buy_links:
            try:
                if a.is_displayed():
                    clickable = a
                    break
            except Exception:
                continue
        if not clickable:
            clickable = buy_links[0]

        try:
            self._random_pre_click_wait("商品页購入手続きへ")
            driver.execute_script("arguments[0].click();", clickable)
            time.sleep(float(self.y_cfg.get("wait_after_buy_click_seconds", 3)))
        except Exception as e:
            return False, self._make_summary(order, failure_reason="点击购买失败: %s" % e)

        # 进入下单页后，若出现卖家限制购买用户（疑似拉黑），则跳过本单并通知飞书（非 3DS）
        try:
            src = driver.page_source or ""
        except Exception:
            src = ""
        seller_restrict_text = "この商品は出品者により「購入」ができるユーザーが制限されています"
        if seller_restrict_text in src:
            msg = (
                "雅虎闲置：该商品被卖家限制购买用户（疑似被拉黑），无法下单。"
                "请人工处理本订单。本单已自动跳过并继续处理后续订单。"
            )
            self.logger.warning("%s item=%s url=%s", msg, item_id, product_url)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg, "商品 item_id=%s url=%s" % (item_id, product_url)],
                    user_id=order.get("user_id"),
                    extra="原因页文案：%s" % seller_restrict_text,
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason="卖家限制购买用户，已跳过（疑似拉黑）",
                runner_pause_requested=False,
            )

        self._dismiss_ok_modal(driver, timeout=float(self.y_cfg.get("modal_ok_seconds", 6)))

        # 后端传价一律以当前页真实金额为准（不用订单接口价 / 商品 API 价）
        price_int, price_err = self._require_page_price(driver, "下单确认页-回调前")
        if not price_int:
            return False, self._make_summary(order, failure_reason=price_err)

        # addedCartCallbackSimple 需要站点特定字段；不要复用骏河屋默认值
        callback_product = dict(product or {})
        callback_product["goods_no"] = (
            str(callback_product.get("goods_no") or callback_product.get("no") or "").strip() or item_id
        )
        callback_product["shop_id"] = self._store_name()
        callback_product["quantity"] = int(callback_product.get("quantity") or 1)
        callback_product["name"] = callback_product.get("name") or (data.get("title") if isinstance(data, dict) else "") or item_id
        callback_product["price"] = price_int

        # addedCartCallbackSimple：与骏河屋一致（单商品）
        try:
            ok_cb, cb_msg = send_added_cart_callback(
                order,
                callback_product,
                config=self.config,
                is_lack=0,
                is_limit=0,
                use_curl=use_curl,
            )
        except Exception as e:
            return False, self._make_summary(order, failure_reason="加购回调异常: %s" % e)
        if not ok_cb:
            msgs = [
                "雅虎闲置：addedCartCallbackSimple 未成功（Message=%s）" % (cb_msg or "-")
            ]
            self._handle_order_issue(order, msgs, reason="加购回调失败")
            return False, self._make_summary(
                order,
                failure_reason="addedCartCallbackSimple 未成功: %s" % (cb_msg or "-"),
            )

        # 确认页：再次取当前页真实价 → 截图 + checkCartGoodsSimple
        price_int, price_err = self._require_page_price(driver, "下单确认页-checkCart前")
        if not price_int:
            return False, self._make_summary(order, failure_reason=price_err)
        callback_product["price"] = price_int
        goods_no_for_check = callback_product["goods_no"]
        goods_list = [
            {
                "No": goods_no_for_check,
                "Num": 1,
                "StoreName": self._store_name(),
                "Price": price_int,
            }
        ]
        shot_path = None
        try:
            shot_path = take_full_page_screenshot(driver)
            screen_url = upload_screenshot_get_url(shot_path, self.config)
            if not screen_url:
                return False, self._make_summary(order, failure_reason="截图上传失败")
            ok_chk, chk_err, chk_raw = check_cart_goods_simple(
                order,
                total=price_int,
                goods_fee=price_int,
                operate_fee=0,
                screenshot_url=screen_url,
                config=self.config,
                goods_list_override=goods_list,
                use_curl=use_curl,
            )
        finally:
            if shot_path:
                try:
                    import os

                    os.remove(shot_path)
                except Exception:
                    pass

        if not ok_chk:
            msgs = ["checkCartGoodsSimple 失败: %s" % chk_err]
            self._handle_order_issue(order, msgs, reason="结算校验失败")
            return False, self._make_summary(
                order,
                failure_reason=chk_err or "checkCartGoodsSimple 失败",
                check_cart_requested=True,
                check_cart_response=(chk_raw or "")[:500],
            )

        # 最终确认
        try:
            self._click_confirm_purchase(driver)
        except Exception as e:
            return False, self._make_summary(
                order,
                failure_reason="点击「購入内容を確認する」失败: %s" % e,
                check_cart_requested=True,
                check_cart_response="ok",
            )

        # 「購入内容を確認する」后也可能直接跳到 404（售出/删除）
        if self._detect_page_not_found(driver):
            gone_msg = (
                "雅虎闲置：点击「購入内容を確認する」后进入 404 页面，"
                "商品可能已被售出或删除，下单可能未成功。"
            )
            cur = ""
            try:
                cur = driver.current_url or ""
            except Exception:
                pass
            self.logger.error("%s order=%s url=%s", gone_msg, order_id, cur)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [
                        gone_msg,
                        "页面文案：ご指定のページが見つかりませんでした",
                        "商品 item_id=%s url=%s" % (item_id, product_url),
                        "当前页面 URL: %s" % cur,
                        "请人工核验：是否已扣款/是否已成交。",
                    ],
                    user_id=order.get("user_id"),
                    extra="雅虎闲置：确认前已 404，本单按失败跳过。",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=gone_msg,
                check_cart_requested=True,
                check_cart_response="ok",
                runner_pause_requested=False,
            )

        # 第二次确认：弹窗点击 OK 才会正式提交
        try:
            self._click_confirm_ok(driver)
        except Exception as e:
            # 弹窗未出现时，再扫一次是否已是 404
            if self._detect_page_not_found(driver):
                gone_msg = (
                    "雅虎闲置：二次确认弹窗未出现且已进入 404，"
                    "商品可能已被售出或删除，下单可能未成功。"
                )
                cur = ""
                try:
                    cur = driver.current_url or ""
                except Exception:
                    pass
                self.logger.error("%s order=%s url=%s", gone_msg, order_id, cur)
                try:
                    self.feishu_notifier.notify_order_issue(
                        str(order_id),
                        [
                            gone_msg,
                            "页面文案：ご指定のページが見つかりませんでした",
                            "商品 item_id=%s url=%s" % (item_id, product_url),
                            "当前页面 URL: %s" % cur,
                            "请人工核验：是否已扣款/是否已成交。",
                        ],
                        user_id=order.get("user_id"),
                        extra="雅虎闲置：确认 OK 前已 404，本单按失败跳过。",
                    )
                except Exception:
                    pass
                return False, self._make_summary(
                    order,
                    failure_reason=gone_msg,
                    check_cart_requested=True,
                    check_cart_response="ok",
                    runner_pause_requested=False,
                )
            return False, self._make_summary(
                order,
                failure_reason="点击二次确认 #confirm_ok 失败: %s" % e,
                check_cart_requested=True,
                check_cart_response="ok",
            )

        outcome, info_after_confirm = self._wait_success_or_seller_restriction(driver, item_id)
        if outcome == "page_not_found":
            gone_msg = (
                "雅虎闲置：最终确认后进入「ページが見つかりません」页面，"
                "商品可能已被售出或删除，下单可能未成功。"
            )
            self.logger.error(
                "%s order=%s item=%s url=%s",
                gone_msg,
                order_id,
                item_id,
                info_after_confirm,
            )
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [
                        gone_msg,
                        "页面文案：ご指定のページが見つかりませんでした",
                        "商品 item_id=%s url=%s" % (item_id, product_url),
                        "当前页面 URL: %s" % (info_after_confirm or ""),
                        "请人工核验：是否已扣款/是否已成交。",
                    ],
                    user_id=order.get("user_id"),
                    extra="雅虎闲置：确认提交后商品页 404，本单按失败跳过，调度继续。",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=gone_msg,
                check_cart_requested=True,
                check_cart_response="ok",
                runner_pause_requested=False,
            )
        if outcome == "seller_restricted":
            skip_msg = (
                "雅虎闲置：卖家限制交易，无法购买（購入できません）。原因：%s。"
                "本单已自动跳过（非 3DS/非支付卡死），调度将继续处理后续订单。"
            ) % (info_after_confirm,)
            self.logger.warning(skip_msg)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [skip_msg, "商品 item_id=%s url=%s" % (item_id, product_url)],
                    user_id=order.get("user_id"),
                    extra="无需按 3DS 排查；本站点任务不会因此暂停。",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason="卖家限制交易，已跳过：%s" % (info_after_confirm[:300]),
                check_cart_requested=True,
                check_cart_response="ok",
                runner_pause_requested=False,
            )

        ok_trade = outcome == "success"
        final_url = info_after_confirm
        stall_msg = ""
        if not ok_trade:
            # 与改二次核验前一致：一超时就发飞书，便于人工反查；二次核验成功时仍会保留本条记录
            stall_msg = (
                "雅虎闲置：二次确认后未在 %s 秒内进入成功页（/item/<id>/order/done 或 成功文案），可能 3DS 或页面异常。当前 URL: %s"
                % (
                    int(self.y_cfg.get("post_confirm_wait_seconds", 120)),
                    final_url,
                )
            )
            self.logger.error(stall_msg)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [stall_msg],
                    user_id=order.get("user_id"),
                    # extra="建议人工检查支付/3DS。本站点 Runner 将暂停。",
                    extra="建议人工检查支付/3DS。本单已跳过，调度将继续处理后续订单。",
                )
            except Exception:
                pass

            recovered, detail_url = try_recover_after_success_page_timeout(
                driver,
                item_id,
                self.y_cfg,
                self.logger,
                item_api_check=self._fetch_item_status,
            )
            if recovered:
                ok_trade = True
                final_url = detail_url or (driver.current_url or "")
                self.logger.info(
                    "雅虎闲置：完成页等待超时，经订单详情二次核验视为已成交，继续后续截图与回调"
                )

        if not ok_trade:
            # 飞书已在上方超时分支发送，此处不再重复
            return False, self._make_summary(
                order,
                failure_reason=stall_msg,
                check_cart_requested=True,
                check_cart_response="ok",
                # runner_pause_requested=bool(
                #     self.y_cfg.get("pause_runner_on_order_stall", True)
                # ),
                runner_pause_requested=False,
            )

        purchase_url = final_url
        purchase_nobs = [{"no": item_id, "url": purchase_url}]
        credit = self._credit_card()

        ok_add, add_err, add_raw = send_add_no_callback(
            order,
            purchase_nobs,
            credit_card=credit,
            config=self.config,
            use_curl=use_curl,
        )
        if not ok_add:
            msgs = ["addNoCallbackSimple 失败: %s" % add_err]
            self._handle_order_issue(order, msgs, reason="完成回调失败")
            return False, self._make_summary(
                order,
                failure_reason=add_err or "addNoCallbackSimple 失败",
                check_cart_requested=True,
                check_cart_response="ok",
                add_no_requested=True,
                add_no_response=(add_raw or "")[:500],
            )

        # updateGoodsNo 传价：再从当前成功页取真实价；若页面无金额则沿用确认页已抓到的价
        page_price2, price_err2 = self._require_page_price(driver, "成功页-updateGoodsNo前")
        if page_price2:
            price_int = page_price2
        else:
            self.logger.warning(
                "雅虎闲置：%s；updateGoodsNo 沿用确认页价格 %s 円",
                price_err2,
                price_int,
            )
        goods_no_list = [{"no": goods_no_for_check, "price": price_int, "num": 1}]
        shot2 = None
        detail_shot_url = ""
        try:
            # 第二张截图前：若详情/完成页实际是 404，仅告警，不中断后续上传与回调
            try:
                page_src = driver.page_source or ""
            except Exception:
                page_src = ""
            if "ご指定のページが見つかりませんでした" in page_src:
                self.logger.warning(
                    "雅虎闲置：成功截图页为 404（ご指定のページが見つかりませんでした），URL=%s",
                    getattr(driver, "current_url", "") or "",
                )
                try:
                    self.feishu_notifier.notify_order_issue(
                        str(order_id),
                        [
                            "全部流程完成，但未成功进入注文成功页面。请人工核验。",
                            "截图页出现：ご指定のページが見つかりませんでした",
                            "当前 URL: %s" % (getattr(driver, "current_url", "") or ""),
                            "item_id=%s" % item_id,
                        ],
                        user_id=order.get("user_id"),
                        extra="雅虎闲置：第二张截图页异常；仍按原流程上传截图并回调。",
                    )
                except Exception:
                    pass
            shot2 = take_full_page_screenshot(driver)
            detail_shot_url = upload_screenshot_get_url(shot2, self.config) or ""
        finally:
            if shot2:
                try:
                    import os

                    os.remove(shot2)
                except Exception:
                    pass

        update_errors: List[str] = []
        if detail_shot_url:
            ok_u, uerr = send_update_goods_no_callback(
                order,
                item_id,
                goods_no_list,
                detail_shot_url,
                self._store_name(),
                self.config,
                use_curl=use_curl,
            )
            if not ok_u:
                update_errors.append(uerr or "updateGoodsNoCallback 失败")
        else:
            update_errors.append("成功页截图上传失败，跳过 updateGoodsNoCallback")

        self.logger.info("雅虎闲置：订单 %s 处理完成，PurchaseNo=%s", order_id, item_id)
        return True, self._make_summary(
            order,
            success=True,
            check_cart_requested=True,
            check_cart_response="ok",
            add_no_requested=True,
            add_no_response=(add_raw or "")[:200],
            update_errors=update_errors,
        )

    def submit_price_consultation(
        self, product_url: str, bargain_yen: int, item_id: str
    ) -> Tuple[bool, str]:
        """
        打开商品页，检测 #fltdscnt 议价窗口，填入 BargainPrice 并提交。
        跳转到 /item/{id}/negotiate 视为提交完成。
        """
        driver = self.browser_manager.get_driver()
        wait_sec = int(self.y_cfg.get("bargain_widget_wait_seconds", 25))
        nego_sec = int(self.y_cfg.get("bargain_negotiate_wait_seconds", 20))
        yen_str = str(int(bargain_yen))
        self.logger.info(
            "雅虎闲置议价：打开商品页提交価格の相談 item=%s yen=%s url=%s",
            item_id,
            yen_str,
            product_url,
        )
        try:
            self.browser_manager.navigate(product_url)
        except Exception as e:
            return False, "打开商品页失败: %s" % e

        widget = None
        try:
            widget = WebDriverWait(driver, wait_sec).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#fltdscnt"))
            )
        except TimeoutException:
            try:
                widget = driver.find_element(
                    By.CSS_SELECTOR,
                    'input[placeholder="購入したい金額を入力"]',
                )
            except Exception:
                widget = None
        if widget is None:
            return False, "商品页未找到议价窗口 #fltdscnt（価格の相談）"

        inp = None
        try:
            inp = driver.find_element(
                By.CSS_SELECTOR, "#fltdscnt input[type='tel']"
            )
        except Exception:
            try:
                inp = driver.find_element(
                    By.CSS_SELECTOR,
                    'input[placeholder="購入したい金額を入力"]',
                )
            except Exception:
                inp = None
        if inp is None:
            return False, "议价窗口内未找到金额输入框"

        try:
            driver.execute_script(
                """
                var el = arguments[0];
                var val = arguments[1];
                el.focus();
                var proto = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                );
                if (proto && proto.set) { proto.set.call(el, val); }
                else { el.value = val; }
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """,
                inp,
                yen_str,
            )
        except Exception as e:
            return False, "填写议价金额失败: %s" % e

        time.sleep(0.4)
        try:
            current = driver.execute_script("return arguments[0].value || '';", inp) or ""
        except Exception:
            current = ""
        if str(current).strip() != yen_str:
            try:
                inp.clear()
                inp.send_keys(yen_str)
            except Exception as e:
                return False, "议价金额未写入输入框: %s" % e

        send_btn = None
        try:
            send_btn = driver.find_element(
                By.CSS_SELECTOR, "#fltdscnt button[data-cl-params*='discount']"
            )
        except Exception:
            try:
                send_btn = driver.find_element(By.CSS_SELECTOR, "#fltdscnt button")
            except Exception:
                send_btn = None
        if send_btn is None:
            return False, "议价窗口内未找到提交按钮"

        deadline = time.time() + 8.0
        while time.time() < deadline:
            try:
                disabled = send_btn.get_attribute("disabled")
                if disabled in (None, "false", "0", ""):
                    break
            except Exception:
                break
            time.sleep(0.2)
        try:
            disabled = send_btn.get_attribute("disabled")
            if disabled not in (None, "false", "0", ""):
                return False, "议价提交按钮仍为 disabled，金额可能未生效"
        except Exception:
            pass

        try:
            self._random_pre_click_wait("価格の相談提交")
            driver.execute_script("arguments[0].click();", send_btn)
        except Exception as e:
            return False, "点击议价提交按钮失败: %s" % e

        try:
            WebDriverWait(driver, nego_sec).until(
                lambda d: "/negotiate" in ((d.current_url or "").lower())
            )
        except TimeoutException:
            cur = ""
            try:
                cur = driver.current_url or ""
            except Exception:
                pass
            return False, "提交后未进入协商页 /negotiate 当前URL=%s" % cur

        self.logger.info(
            "雅虎闲置议价：已进入协商页 URL=%s",
            driver.current_url or "",
        )
        return True, ""

    def _handle_order_issue(self, order: Dict[str, Any], messages: List[str], reason: str = "") -> None:
        order_id = order.get("order_id", "未知")
        user_id = order.get("user_id")
        if not messages:
            return
        if reason:
            self.logger.warning("订单 %s %s:", order_id, reason)
        for msg in messages:
            self.logger.warning("  - %s", msg)
        try:
            self.ticket_creator.create_ticket(order_id, messages, user_id=user_id)
        except Exception as e:
            self.logger.error("创建工单失败: %s", e)
        try:
            extra = (reason + "，已创建工单。") if reason else "已创建工单。"
            self.feishu_notifier.notify_order_issue(order_id, messages, user_id=user_id, extra=extra)
        except Exception as e:
            self.logger.warning("飞书提醒失败: %s", e)
