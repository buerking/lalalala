# -*- coding: utf-8 -*-
"""
乐天书店（books.rakuten.co.jp）：多商品 → 购物车 → 结算确认 → 一键注文確定 → 注文完了。

后端回调与乐天市场对齐（同一套接口，仅用拉单凭证）：
  getOrderListSimple 拉单 Mark/Secret
  → 按 List 行加购 → 每行 addedCartCallbackSimple
  → 确认页金额 + checkCartGoodsSimple
  → 注文確定 → addNoCallbackSimple → updateGoodsNoCallback

不再调用已禁用的 getOrderSimple 刷新 Mark。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
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
from src.auth.rakuten_session import RakutenLoginError, RakutenSessionGuard
from src.utils.logger import LoggerMixin

_RB_ID_RE = re.compile(r"/rb/(?P<id>\d+)", re.IGNORECASE)
_BOOK_SHOP_ID_RE = re.compile(
    r"item\.rakuten\.co\.jp/book/(?P<id>\d+)", re.IGNORECASE
)
_BOOK_SHOP_ID_ENCODED_RE = re.compile(
    r"item\.rakuten\.co\.jp(?:/|%2[Ff])book(?:/|%2[Ff])(?P<id>\d+)",
    re.IGNORECASE,
)


def extract_rakuten_book_id(url: str) -> Optional[str]:
    """从 books.../rb/{id}、item.../book/{id} 或联盟 pc 参数中提取书店商品 ID。"""
    if not url:
        return None
    from urllib.parse import parse_qs, unquote, urlparse

    candidates = [str(url).strip()]
    try:
        candidates.append(unquote(candidates[0]))
    except Exception:
        pass
    try:
        parsed = urlparse(candidates[0])
        if "afl.rakuten.co.jp" in (parsed.netloc or "").lower():
            qs = parse_qs(parsed.query)
            for key in ("pc", "m"):
                val = (qs.get(key) or [""])[0].strip()
                if not val:
                    continue
                candidates.append(val)
                try:
                    candidates.append(unquote(val))
                except Exception:
                    pass
    except Exception:
        pass

    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        m = _RB_ID_RE.search(c)
        if m:
            return m.group("id")
        m2 = _BOOK_SHOP_ID_RE.search(c)
        if m2:
            return m2.group("id")
        m3 = _BOOK_SHOP_ID_ENCODED_RE.search(c)
        if m3:
            return m3.group("id")
    return None


def normalize_rakuten_books_product_url(url: str) -> str:
    """统一为 books.rakuten.co.jp/rb/{id}/，便于书店加购页选择器生效。"""
    raw = (url or "").strip()
    bid = extract_rakuten_book_id(raw)
    if bid:
        return "https://books.rakuten.co.jp/rb/%s/" % bid
    return raw


def _parse_yen_int(text: str) -> int:
    if not text:
        return 0
    s = re.sub(r"[^\d]", "", str(text).strip())
    return int(s) if s else 0


class RakutenBooksOrderProcessor(LoggerMixin):
    def __init__(self, config: Dict[str, Any], browser_manager: BrowserManager):
        self.config = config
        self.browser_manager = browser_manager
        self.rb_cfg = config.get("rakuten_books") or {}
        self.ticket_creator = TicketCreator(config)
        self.feishu_notifier = FeishuNotifier(config)
        self.session_guard = (
            RakutenSessionGuard(browser_manager, config)
            if RakutenSessionGuard.is_enabled(config)
            else None
        )

    def _navigate(self, driver, url: str) -> None:
        target = (url or "").split("#")[0]
        BrowserManager.navigate_allow_timeout(driver, target, self.logger)
        self._ensure_session_after_nav(driver, resume_url=target)

    def _ensure_rakuten_session(self, resume_url=None) -> None:
        if not self.session_guard:
            return
        self.session_guard.ensure_logged_in(resume_url=resume_url)

    def _ensure_session_after_nav(self, driver, resume_url: Optional[str] = None) -> None:
        """任意跳转后：若落在登录站则自动填密，再回到业务页。"""
        target = (resume_url or "").strip()
        if not target:
            try:
                target = (driver.current_url or "").strip()
            except Exception:
                target = ""
        # 登录站本身不要当 resume 目标
        try:
            low = target.lower()
            if any(
                h in low
                for h in (
                    "login.account.rakuten",
                    "login.rakuten.co.jp",
                    "member.id.rakuten",
                    "glogin.rakuten",
                    "id.rakuten.co.jp",
                )
            ):
                target = ""
        except Exception:
            pass
        self._ensure_rakuten_session(resume_url=target or None)

    def _ensure_session_after_action(
        self, resume_url: Optional[str] = None, wait_seconds: float = 3.0
    ) -> None:
        """点击確定/次へ后可能异步跳到 session/upgrade。"""
        if not self.session_guard:
            return
        target = (resume_url or "").strip()
        try:
            low = target.lower()
            if any(
                h in low
                for h in (
                    "login.account.rakuten",
                    "login.rakuten.co.jp",
                    "member.id.rakuten",
                    "glogin.rakuten",
                )
            ):
                target = ""
        except Exception:
            pass
        self.session_guard.ensure_after_possible_redirect(
            resume_url=target or None, wait_seconds=min(float(wait_seconds), 1.5)
        )


    def _random_pre_click_wait(self, action: str) -> None:
        pay_cfg = self.config.get("payment") or {}
        rng = self.rb_cfg.get("pre_click_wait_seconds_range") or pay_cfg.get(
            "pre_click_wait_seconds_range", [0.7, 1.8]
        )
        try:
            mn = float(rng[0])
            mx = float(rng[1])
        except Exception:
            mn, mx = 0.7, 1.8
        sec = random.uniform(min(mn, mx), max(mn, mx))
        self.logger.info("乐天书店：关键点击前随机等待 %.2f 秒（%s）", sec, action)
        time.sleep(sec)

    def _is_ichiba_handoff(self) -> bool:
        return bool(self.rb_cfg.get("handoff_from_ichiba"))

    def _pull_site_from_order(self, order: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """拉单站身份快照（市场→书店转交流程时写入 order._pull_site）。"""
        snap = {}
        if isinstance(order, dict):
            raw = order.get("_pull_site")
            if isinstance(raw, dict):
                snap = raw
        return {
            "pc_mark": str(snap.get("pc_mark") or "").strip(),
            "store_name": str(snap.get("store_name") or "").strip(),
            "credit_card": str(snap.get("credit_card") or "").strip(),
        }

    def _store_name(self, order: Optional[Dict[str, Any]] = None) -> str:
        snap = self._pull_site_from_order(order)
        if snap.get("store_name"):
            return snap["store_name"]
        if self._is_ichiba_handoff():
            return (
                self.rb_cfg.get("pull_store_name")
                or self.rb_cfg.get("store_name")
                or "乐天市场"
            ).strip()
        return (self.rb_cfg.get("store_name") or "乐天书店").strip()

    def _credit_card_label(self, order: Optional[Dict[str, Any]] = None) -> str:
        snap = self._pull_site_from_order(order)
        if snap.get("credit_card"):
            return snap["credit_card"]
        if self._is_ichiba_handoff():
            # 转交时绝不能落到独立书店默认 rakuten_books
            return (
                self.rb_cfg.get("pull_credit_card")
                or self.rb_cfg.get("add_no_credit_card")
                or ((self.config.get("payment") or {}).get("add_no_credit_card") or "")
                or "8828"
            ).strip()
        return (self.rb_cfg.get("add_no_credit_card") or "rakuten_books").strip()

    def _ensure_callback_pc_mark(self, order: Optional[Dict[str, Any]] = None) -> str:
        """保证 config.order_api.pc_mark 为拉单站；转交时强制 rakuten。"""
        api = dict(self.config.get("order_api") or {})
        snap = self._pull_site_from_order(order)
        want = (
            snap.get("pc_mark")
            or (self.rb_cfg.get("pull_pc_mark") if self._is_ichiba_handoff() else "")
            or (api.get("pc_mark") or "")
        ).strip()
        if self._is_ichiba_handoff() and not want:
            want = "rakuten"
        if want and api.get("pc_mark") != want:
            api["pc_mark"] = want
            self.config["order_api"] = api
            self.logger.info("乐天书店：回调 PcMark 锁定为拉单站 %s", want)
        return (api.get("pc_mark") or want or "").strip()

    def _log_callback_identity(self, phase: str, order: Dict[str, Any]) -> None:
        api = self.config.get("order_api") or {}
        self.logger.info(
            "乐天书店：%s 回调身份 PcMark=%s StoreName=%s CreditCard=%s handoff=%s",
            phase,
            api.get("pc_mark") or "",
            self._store_name(order),
            self._credit_card_label(order),
            self._is_ichiba_handoff(),
        )

    def _refresh_order_mark(self, order: Dict[str, Any], *, reason: str = "") -> str:
        """已废弃：getOrderSimple 已禁用。保留空实现以免旧调用报错。"""
        _ = (order, reason)
        return ""

    def _make_summary(
        self,
        order: Dict[str, Any],
        *,
        success: bool = False,
        failure_reason: str = "",
        payment_method: str = "rakuten_one_click",
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

    def _cart_item_count(self, driver) -> int:
        """以商品行 / js-itemNum 为准；勿用 page_source 搜空车文案（隐藏 DOM 常驻）。"""
        try:
            el = driver.find_element(By.CSS_SELECTOR, "#js-itemNum")
            text = (el.text or "").strip()
            if text.isdigit():
                return int(text)
        except Exception:
            pass
        try:
            rows = driver.find_elements(
                By.CSS_SELECTOR, ".item-list .js-item, .item-list .item.selected, .item-list .item"
            )
            return len(rows or [])
        except Exception:
            return -1

    def _clear_cart(self, driver) -> None:
        cart_url = (self.rb_cfg.get("cart_url") or "").strip() or (
            "https://books.step.rakuten.co.jp/rms/mall/book/bs/Cart"
        )
        self._navigate(driver, cart_url.split("#")[0])
        time.sleep(float(self.rb_cfg.get("wait_after_cart_load_seconds", 2)))
        n = self._cart_item_count(driver)
        if n == 0:
            self.logger.info("乐天书店：购物车已为空（itemNum=0），跳过清空")
            return
        if n < 0:
            self.logger.info("乐天书店：未能读取购物车件数，仍尝试清空按钮")
        else:
            self.logger.info("乐天书店：购物车有 %s 件，执行清空", n)
        sel = (self.rb_cfg.get("clear_cart_button_css") or "button[name=basket_clear]").strip()
        try:
            btn = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(2)
            n2 = self._cart_item_count(driver)
            if n2 == 0:
                self.logger.info("乐天书店：清空购物车完成")
            elif n2 > 0:
                self.logger.warning("乐天书店：清空后仍有 %s 件", n2)
        except Exception as e:
            # 仅当确认已空才当作可忽略
            if self._cart_item_count(driver) == 0:
                self.logger.info("乐天书店：无清空按钮且购物车为空")
            else:
                self.logger.warning("乐天书店：清空购物车失败: %s", e)
                raise

    def _add_product_to_cart(self, driver, product_url: str, quantity: int) -> None:
        self._navigate(driver, product_url.split("?")[0].rstrip("/") + "/")
        time.sleep(float(self.rb_cfg.get("wait_after_pdp_load_seconds", 2)))
        units_sel = (self.rb_cfg.get("units_select_css") or "select#units").strip()
        add_btn = (self.rb_cfg.get("add_to_cart_button_css") or "button.new_addToCart").strip()
        try:
            sel_el = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, units_sel))
            )
            Select(sel_el).select_by_value(str(max(1, min(100, int(quantity or 1)))))
        except Exception:
            pass
        self._random_pre_click_wait("買い物かごに入れる")
        btn = WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.CSS_SELECTOR, add_btn)))
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(float(self.rb_cfg.get("wait_after_add_cart_seconds", 3)))

    def _is_books_confirm_page(self, driver) -> bool:
        """是否已在注文内容の確認（可点「注文を確定する」）。"""
        for el in driver.find_elements(
            By.CSS_SELECTOR, "button.btn-red[name='commit_order'], button[name='commit_order']"
        ):
            try:
                if el.is_displayed():
                    return True
            except Exception:
                continue
        try:
            title = (driver.title or "").strip()
            if "注文内容の確認" in title:
                return True
        except Exception:
            pass
        try:
            src = driver.page_source or ""
        except Exception:
            return False
        return "注文内容の確認" in src and "name=\"commit_order\"" in src.replace("'", '"')

    def _find_books_commit_button(self, driver, commit_sel: str = ""):
        """查找确认页红色「注文を確定する」（侧栏 float 也可能挡住 clickable 判定）。"""
        sels = [
            s.strip()
            for s in str(commit_sel or "").split(",")
            if s.strip()
        ] or [
            "button.btn-red[name='commit_order']",
            "button[name='commit_order']",
            "form[action*='ConfirmOrderFork'] button[name='commit_order']",
            ".js-float-box button[name='commit_order']",
            ".area-cost-detail button[name='commit_order']",
        ]
        for sel in sels:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue
        try:
            for el in driver.find_elements(
                By.XPATH,
                "//button[@name='commit_order' or contains(normalize-space(.),'注文を確定する')]",
            ):
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _click_books_commit_order(self, driver, commit_sel: str = "") -> None:
        """
        点击书店确认页「注文を確定する」。
        不依赖 element_to_be_clickable（右侧绝对定位浮层常导致误判不可点）。
        """
        self._random_pre_click_wait("注文を確定する")
        last_err = ""
        for attempt in range(1, 4):
            self._ensure_session_after_nav(driver)
            if not self._is_books_confirm_page(driver):
                # 已离开确认页视为成功
                if self._is_books_success_page(driver):
                    return
                self._pass_books_checkout_intermediates(driver)

            btn = self._find_books_commit_button(driver, commit_sel)
            if btn is None:
                last_err = "未找到「注文を確定する」按钮"
                self.logger.warning("乐天书店：%s（第 %s 次）", last_err, attempt)
                time.sleep(1.5)
                continue

            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
                    btn,
                )
            except Exception:
                pass
            time.sleep(0.35)

            clicked = False
            try:
                driver.execute_script("arguments[0].click();", btn)
                clicked = True
                self.logger.info(
                    "乐天书店：JS 点击注文を確定する（第 %s 次）", attempt
                )
            except Exception as e:
                last_err = "JS click 失败: %s" % e
            if not clicked:
                try:
                    btn.click()
                    clicked = True
                    self.logger.info(
                        "乐天书店：原生点击注文を確定する（第 %s 次）", attempt
                    )
                except Exception as e:
                    last_err = "原生 click 失败: %s" % e
            if not clicked:
                # 带 commit_order 字段提交表单（与按钮 name/value 一致）
                try:
                    ok = driver.execute_script(
                        """
                        var b = document.querySelector("button[name='commit_order']");
                        if (!b) return false;
                        var f = b.form || b.closest('form');
                        if (!f) { b.click(); return true; }
                        var h = document.createElement('input');
                        h.type = 'hidden';
                        h.name = 'commit_order';
                        h.value = 'true';
                        f.appendChild(h);
                        f.submit();
                        return true;
                        """
                    )
                    if ok:
                        clicked = True
                        self.logger.info(
                            "乐天书店：表单 submit(commit_order)（第 %s 次）", attempt
                        )
                    else:
                        last_err = "表单 submit 未找到 commit_order"
                except Exception as e:
                    last_err = "表单 submit 失败: %s" % e

            if not clicked:
                time.sleep(1.2)
                continue

            # 给跳转一点时间；确定后常出现 session/upgrade
            time.sleep(1.2)
            self._ensure_session_after_action(wait_seconds=1.0)
            if self._is_books_success_page(driver):
                return
            if not self._is_books_confirm_page(driver):
                self.logger.info("乐天书店：已离开注文確認页")
                return
            self.logger.warning(
                "乐天书店：点击注文を確定する后仍在确认页，准备重试（%s/3）",
                attempt,
            )
            time.sleep(1.5)

        raise RuntimeError(
            last_err or "多次点击「注文を確定する」后仍停留在确认页"
        )

    def _is_books_success_page(self, driver) -> bool:
        """注文完了（step5 thankyou）页，或已进入注文・配送状況の確認。"""
        try:
            title = (driver.title or "").strip()
            if "注文完了" in title:
                return True
            # 用户提供的最终详情页 title
            if "注文配送状況の確認" in title or "注文・配送状況の確認" in title:
                return True
        except Exception:
            pass
        try:
            if driver.find_elements(By.CSS_SELECTOR, "div.order-number"):
                return True
        except Exception:
            pass
        # 配送状況確認页：有注文番号即可视为已下单成功
        if self._is_books_delivery_status_page(driver):
            return True
        try:
            el = driver.find_element(By.CSS_SELECTOR, "#ratOrderId")
            if (el.get_attribute("value") or "").strip():
                return True
        except Exception:
            pass
        try:
            src = driver.page_source or ""
        except Exception:
            return False
        success_kw = (self.rb_cfg.get("success_page_text") or "注文完了").strip()
        markers = (
            success_kw,
            "ご注文ありがとうございました",
            "step5_purchase_complete",
            "cart__main__step5",
            'id="ratOrderId"',
            "注文・配送状況の確認",
            "注文配送状況の確認",
            'class="order-info__number"',
            'name="rms_order_number"',
        )
        return any(m and m in src for m in markers)

    def _is_books_delivery_status_page(self, driver) -> bool:
        """
        Myページ「注文・配送状況の確認」详情页（截图目标页）。
        特征见用户提供的 HTML：.order-info__number / input[name=rms_order_number]
        """
        try:
            for el in driver.find_elements(
                By.CSS_SELECTOR,
                "span.order-info__number, input[name='rms_order_number']",
            ):
                try:
                    if el.tag_name.lower() == "input":
                        val = (el.get_attribute("value") or "").strip()
                    else:
                        val = (el.text or "").strip()
                    if val and re.search(r"\d{6}-\d{8}-\d+", val):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        try:
            title = (driver.title or "").strip()
            if "配送状況" in title and "確認" in title:
                return True
        except Exception:
            pass
        try:
            src = driver.page_source or ""
        except Exception:
            return False
        return ("注文・配送状況の確認" in src or "注文配送状況の確認" in src) and (
            "order-info__number" in src or 'name="rms_order_number"' in src
        )

    def _extract_books_order_info(self, driver) -> Tuple[str, str]:
        """
        从注文完了页或配送状況確認页解析注文番号与详情 URL。
        """
        purchase_no = ""
        detail_url = ""
        back_number = ""

        # 0) 配送状況確認页（用户提供的最终 HTML）
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, "span.order-info__number"):
                t = (el.text or "").strip()
                if re.search(r"\d{6}-\d{8}-\d+", t):
                    purchase_no = t
                    break
        except Exception:
            pass
        try:
            el = driver.find_element(By.CSS_SELECTOR, "input[name='rms_order_number']")
            v = (el.get_attribute("value") or "").strip()
            if v:
                purchase_no = purchase_no or v
        except Exception:
            pass
        try:
            el = driver.find_element(By.CSS_SELECTOR, "input[name='back_number']")
            back_number = (el.get_attribute("value") or "").strip()
        except Exception:
            pass
        if purchase_no and back_number and not detail_url:
            detail_url = (
                "https://books.rakuten.co.jp/mypage/delivery/status"
                "?order_number=%s&back_number=%s" % (purchase_no, back_number)
            )

        # 1) 変更・確認 按钮
        try:
            for el in driver.find_elements(
                By.CSS_SELECTOR, "div.order-number a.white-btn, a.white-btn"
            ):
                try:
                    txt = (el.text or "").strip()
                    href = (el.get_attribute("href") or "").strip()
                    if "ご注文内容の変更" in txt or "変更・確認" in txt:
                        if href:
                            detail_url = detail_url or href
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # 2) 注文番号链接文字 + href
        num_sel = (self.rb_cfg.get("order_number_css") or "div.order-number dd a").strip()
        try:
            el = driver.find_element(By.CSS_SELECTOR, num_sel)
            purchase_no = purchase_no or (el.text or "").strip()
            href = (el.get_attribute("href") or "").strip()
            if href and not detail_url:
                detail_url = href
        except Exception:
            pass

        # 3) RAT / 正文兜底
        if not purchase_no:
            try:
                el = driver.find_element(By.CSS_SELECTOR, "#ratOrderId")
                purchase_no = (el.get_attribute("value") or "").strip()
            except Exception:
                pass
        if not purchase_no:
            try:
                m = re.search(r"(\d{6}-\d{8}-\d{10})", driver.page_source or "")
                if m:
                    purchase_no = m.group(1)
            except Exception:
                pass

        if detail_url:
            detail_url = detail_url.replace("&amp;", "&")
        return purchase_no, detail_url

    def _open_books_order_detail_for_screenshot(self, driver, detail_url: str) -> None:
        """
        打开注文详情页再截图（用户要求：先点「ご注文内容の変更・確認」）。
        目标页：注文・配送状況の確認（.order-info__number / rms_order_number）。
        """
        wait_s = float(self.rb_cfg.get("wait_after_detail_load_seconds", 3))
        clicked = False
        try:
            for el in driver.find_elements(
                By.XPATH,
                "//a[contains(@class,'white-btn') and contains(.,'ご注文内容の変更')]"
                " | //div[contains(@class,'order-number')]//a[contains(.,'ご注文内容の変更')]"
                " | //a[contains(.,'ご注文内容の変更・確認')]",
            ):
                try:
                    if not el.is_displayed():
                        continue
                    href = (el.get_attribute("href") or "").strip()
                    self.logger.info("乐天书店：打开ご注文内容の変更・確認 %s", href or "(click)")
                    driver.execute_script("arguments[0].click();", el)
                    clicked = True
                    break
                except Exception:
                    continue
        except Exception:
            pass

        if not clicked:
            url = (detail_url or "").strip()
            if not url:
                raise RuntimeError("无注文详情 URL，无法打开変更・確認页")
            self.logger.info("乐天书店：直连注文详情 %s", url)
            self._navigate(driver, url)

        time.sleep(max(1.0, wait_s))
        # 等待配送状況確認页关键节点出现（再截图）
        deadline = time.time() + max(12.0, wait_s + 8)
        while time.time() < deadline:
            if self._is_books_delivery_status_page(driver):
                self.logger.info("乐天书店：已进入注文・配送状況の確認页，准备截图")
                time.sleep(0.8)
                return
            try:
                url = (driver.current_url or "").lower()
                if "delivery/status" in url or (
                    "mypage" in url and "order_number=" in url
                ):
                    # URL 已到详情，再等 DOM
                    if driver.find_elements(
                        By.CSS_SELECTOR,
                        "span.order-info__number, .order-info, input[name='rms_order_number']",
                    ):
                        self.logger.info("乐天书店：详情页 DOM 已就绪，准备截图")
                        time.sleep(0.8)
                        return
            except Exception:
                pass
            time.sleep(0.5)
        self.logger.warning(
            "乐天书店：等待配送状況確認页超时，仍尝试截图 URL=%s",
            getattr(driver, "current_url", ""),
        )

    def _pass_books_checkout_intermediates(self, driver) -> None:
        """
        购物车「ご購入手続き」后，可能先停在「支払いと配送」等中间页。
        自动点「次へ」类按钮直到出现注文確認页。
        """
        max_rounds = int(self.rb_cfg.get("checkout_intermediate_max_rounds", 3) or 3)
        for round_idx in range(max(1, max_rounds)):
            if self._is_books_confirm_page(driver):
                if round_idx == 0:
                    self.logger.debug("乐天书店：已在注文確認页")
                else:
                    self.logger.info("乐天书店：已进入注文確認页")
                return

            next_btn = None
            # 优先：文案为「次へ」/进入确认
            xpaths = (
                "//button[normalize-space(.)='次へ' or contains(normalize-space(.),'次へ')]",
                "//input[@type='submit' and (contains(@value,'次へ') or contains(@value,'確認'))]",
                "//a[normalize-space(.)='次へ' or contains(normalize-space(.),'次へ')]",
                "//button[contains(normalize-space(.),'注文内容の確認')]",
                "//button[contains(@class,'btn-red') and not(@name='commit_order')]",
            )
            for xp in xpaths:
                for el in driver.find_elements(By.XPATH, xp):
                    try:
                        if not el.is_displayed() or not el.is_enabled():
                            continue
                        name = (el.get_attribute("name") or "").strip()
                        # 避免点到变更/清空类按钮
                        if name in (
                            "edit_sender",
                            "edit_delivery",
                            "edit_payment",
                            "edit_wrapping",
                            "edit_orderer",
                            "edit_quantity",
                            "basket_clear",
                            "commit_order",
                        ):
                            continue
                        next_btn = el
                        break
                    except Exception:
                        continue
                if next_btn is not None:
                    break

            if next_btn is None:
                self.logger.warning(
                    "乐天书店：未在注文確認页且未找到「次へ」类按钮（URL=%s）",
                    getattr(driver, "current_url", ""),
                )
                return

            self.logger.info(
                "乐天书店：检测到结算中间页，点击下一步（第 %s 次）", round_idx + 1
            )
            self._random_pre_click_wait("书店结算次へ")
            try:
                driver.execute_script("arguments[0].click();", next_btn)
            except Exception:
                next_btn.click()
            time.sleep(float(self.rb_cfg.get("wait_after_checkout_seconds", 4)))
            # 中间页跳转后可能被踢到统一登录 / session/upgrade
            self._ensure_session_after_action(wait_seconds=1.0)

        if not self._is_books_confirm_page(driver):
            raise RuntimeError(
                "未能进入乐天书店注文確認页（仍停在中间步骤），URL=%s"
                % (getattr(driver, "current_url", "") or "")
            )

    def _rat_field_values(self, driver, field_id: str) -> List[str]:
        """读取确认页 RAT 隐藏域（如 ratItemManageNo / ratItemPrice）。"""
        out: List[str] = []
        for sel in ("#%s" % field_id, "input#%s" % field_id):
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                raw = (el.get_attribute("value") or "").strip()
                if not raw:
                    continue
                out.extend([x.strip() for x in re.split(r"\s*,\s*", raw) if x.strip()])
                break
            except Exception:
                continue
        return out

    def _rat_manage_nos(self, driver) -> List[str]:
        """确认页 RAT 隐藏域中的书店商品管理号（常即 /rb/{id}）。"""
        out: List[str] = []
        for x in self._rat_field_values(driver, "ratItemManageNo"):
            # 兼容 18681632 或 213310/22002624
            if "/" in x:
                x = x.split("/")[-1].strip()
            if x.isdigit():
                out.append(x)
        seen = set()
        uniq: List[str] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    def _parse_books_confirm_totals(self, driver) -> Tuple[int, int, int]:
        """
        乐天书店确认页（注文内容の確認）金额：
          - 総合計 → Total（dl.cost-detail__head）
          - 商品合計 → GoodsFee（#js-totalPrice）
          - 送料 / ポイント利用 → OperateFee 组成
        与乐天市场（小計 / 支払い金額 / number-display）结构不同，不可混用。
        """
        goods_fee = 0
        shipping = 0
        point_use = 0
        total = 0

        try:
            tp = driver.find_element(By.CSS_SELECTOR, "#js-totalPrice")
            goods_fee = _parse_yen_int(tp.text)
        except Exception:
            pass

        try:
            head_dd = driver.find_element(By.CSS_SELECTOR, "dl.cost-detail__head dd")
            total = _parse_yen_int(head_dd.text)
        except Exception:
            pass

        # 侧栏明细：商品合計 / 送料 / ポイント利用
        try:
            rows = driver.find_elements(By.CSS_SELECTOR, "ul.cost-detail__body li")
        except Exception:
            rows = []
        for row in rows:
            try:
                txt = (row.text or "").replace("\n", " ").strip()
            except Exception:
                continue
            if not txt:
                continue
            if "商品合計" in txt and goods_fee <= 0:
                goods_fee = _parse_yen_int(txt)
            elif "送料" in txt:
                if "送料無料" in txt:
                    shipping = 0
                else:
                    shipping = _parse_yen_int(txt)
            elif "ポイント利用" in txt:
                if "利用なし" in txt or "なし" in txt:
                    point_use = 0
                else:
                    # 常见「-123円」或「123円」
                    point_use = abs(_parse_yen_int(txt))

        # RAT 兜底总价
        if total <= 0:
            for raw in self._rat_field_values(driver, "ratTotalPrice"):
                total = _parse_yen_int(raw)
                if total > 0:
                    break

        if goods_fee <= 0 and total > 0 and shipping == 0 and point_use == 0:
            goods_fee = total
        if total <= 0 and goods_fee > 0:
            total = goods_fee + shipping - point_use

        operate_fee = int(shipping) - int(point_use)
        # 后端要求 GoodsFee + OperateFee == Total
        if total > 0 and goods_fee > 0 and goods_fee + operate_fee != total:
            operate_fee = total - goods_fee

        return goods_fee, operate_fee, total

    @staticmethod
    def _api_goods_id_and_no(product: Dict[str, Any]) -> Tuple[str, str]:
        """优先用 getOrderListSimple 行的 GoodsId/GoodsNo；勿用 URL 书号覆盖 GoodsId。"""
        gid = str(product.get("goods_id") or product.get("GoodsId") or "").strip()
        gno = str(
            product.get("goods_no")
            or product.get("no")
            or product.get("GoodsNo")
            or ""
        ).strip()
        return gid, gno

    def _goods_list_from_order_products(
        self,
        products: List[Dict[str, Any]],
        order: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        用订单接口行的 GoodsNo（后端对账键）组装 GoodsList。
        市场→书店转交时尤其重要：确认页 DOM 常只有书目 ID/无链接，对不上接口 GoodsNo。
        """
        store = self._store_name(order)
        out: List[Dict[str, Any]] = []
        for p in products or []:
            lines = list(p.get("_source_lines") or [p])
            for line in lines:
                _gid, no = self._api_goods_id_and_no(line)
                if not no:
                    no = (
                        extract_rakuten_book_id(str(line.get("url") or ""))
                        or str(line.get("goods_id") or "").strip()
                    )
                if not no:
                    continue
                try:
                    price = int(round(float(line.get("price") or 0)))
                except Exception:
                    price = 0
                qty = max(1, int(line.get("quantity") or 1))
                out.append(
                    {
                        "No": no,
                        "Num": qty,
                        "StoreName": store,
                        "Price": price,
                    }
                )
        return out

    def _parse_goods_list_from_confirm_page(
        self, driver
    ) -> Tuple[List[Dict[str, Any]], int, int, int]:
        """
        从乐天书店「注文内容の確認」解析 GoodsList + 金额。

        页面结构（books.step）：
          - 商品块：.item-part（单价 .price.txt-red span，数量 span.quantity）
          - 确认页常无商品链接，No 优先取 #ratItemManageNo
          - 合计：総合計 / 商品合計(#js-totalPrice) / 送料
        """
        goods: List[Dict[str, Any]] = []
        store = self._store_name()
        manage_nos = self._rat_manage_nos(driver)
        rat_prices = [
            _parse_yen_int(x) for x in self._rat_field_values(driver, "ratItemPrice")
        ]
        rat_counts = []
        for x in self._rat_field_values(driver, "ratItemCount"):
            try:
                rat_counts.append(int(float(x)))
            except Exception:
                rat_counts.append(0)

        try:
            parts = driver.find_elements(
                By.CSS_SELECTOR, ".cart__main__item .item-part, .item-list .item-part"
            )
        except Exception:
            parts = []

        for idx, part in enumerate(parts):
            bid = ""
            try:
                link = part.find_element(By.CSS_SELECTOR, ".item-info a, a[href*='/rb/']")
                href = (link.get_attribute("href") or "").strip()
                bid = extract_rakuten_book_id(href) or ""
            except Exception:
                href = ""

            if not bid and idx < len(manage_nos):
                bid = manage_nos[idx]
            if not bid and len(parts) == 1 and len(manage_nos) == 1:
                bid = manage_nos[0]

            if not bid:
                # 图片柜路径偶发含商品 CD，仅作最后兜底（优先 manage no）
                try:
                    img = part.find_element(By.CSS_SELECTOR, ".item-img img")
                    src = (img.get_attribute("src") or "") + " " + (
                        img.get_attribute("alt") or ""
                    )
                    m = re.search(r"/rb/(\d+)", src)
                    if m:
                        bid = m.group(1)
                except Exception:
                    pass

            price = 0
            try:
                price_el = part.find_element(
                    By.CSS_SELECTOR, ".price-unit .price span, .price.txt-red span"
                )
                price = _parse_yen_int(price_el.text)
            except Exception:
                price = 0
            if price <= 0 and idx < len(rat_prices):
                price = rat_prices[idx]

            num = 1
            try:
                qty_el = part.find_element(By.CSS_SELECTOR, "span.quantity")
                num = int(re.sub(r"[^\d]", "", (qty_el.text or "1").strip()) or "1")
            except Exception:
                num = 1
            if num <= 0 and idx < len(rat_counts) and rat_counts[idx] > 0:
                num = rat_counts[idx]
            if num <= 0:
                num = 1

            if not bid:
                self.logger.warning(
                    "乐天书店：确认页第 %s 件无法解析 bookId，仍保留价格数量供对账",
                    idx + 1,
                )
                # 用占位 No，避免整行丢弃导致 GoodsFee 对不上
                bid = "unknown_%s" % (idx + 1)

            goods.append({"No": bid, "Num": num, "StoreName": store, "Price": price})

        # DOM 无 .item-part 时：完全走 RAT
        if not goods and manage_nos:
            for i, bid in enumerate(manage_nos):
                price = rat_prices[i] if i < len(rat_prices) else 0
                num = rat_counts[i] if i < len(rat_counts) and rat_counts[i] > 0 else 1
                goods.append(
                    {"No": bid, "Num": num, "StoreName": store, "Price": price}
                )

        goods_fee, operate_fee, total = self._parse_books_confirm_totals(driver)

        line_sum = sum(int(g.get("Price") or 0) * int(g.get("Num") or 1) for g in goods)
        # 侧栏「商品合計」优先；缺失或与行合计几乎一致时用行合计
        if goods_fee <= 0 and line_sum > 0:
            goods_fee = line_sum
        elif line_sum > 0 and abs(line_sum - goods_fee) <= 1:
            goods_fee = line_sum
        if total <= 0:
            total = goods_fee + operate_fee
        if total > 0 and goods_fee > 0 and goods_fee + operate_fee != total:
            operate_fee = total - goods_fee

        self.logger.info(
            "乐天书店：确认页解析 goods=%s GoodsFee=%s OperateFee=%s Total=%s",
            len(goods),
            goods_fee,
            operate_fee,
            total,
        )
        return goods, total, goods_fee, operate_fee

    def process_order(self, order: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        order_id = order.get("order_id", "未知")
        products: List[Dict[str, Any]] = order.get("products") or []
        self.logger.info("乐天书店：开始处理订单 %s，商品数 %s", order_id, len(products))
        if not products:
            return False, self._make_summary(order, failure_reason="订单无商品")

        # 市场转交：一开始就锁死拉单站 PcMark，避免后续回调落到独立书店默认
        self._ensure_callback_pc_mark(order)
        if self._is_ichiba_handoff():
            self._log_callback_identity("开单", order)
        # 与乐天市场一致：只用 getOrderListSimple 的 Mark/Secret，不调 getOrderSimple
        self.logger.info(
            "乐天书店：使用拉单 Mark=%s secret_len=%s（不刷新 getOrderSimple）",
            str(order.get("mark") or "")[:24],
            len(str(order.get("secret") or "")),
        )
        self._log_callback_identity("加购前回调身份", order)

        driver = self.browser_manager.get_driver()
        use_curl = (self.config.get("order_api") or {}).get("use_curl_for_order_api", True)

        try:
            driver = self.browser_manager.ensure_alive(restart_if_dead=True)
        except Exception as e:
            msg = "浏览器窗口不可用，无法继续: %s" % e
            self.logger.error("乐天书店：%s order=%s", msg, order_id)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="乐天书店：Chrome 窗口已关闭，请检查浏览器后重试。",
                )
            except Exception:
                pass
            return False, self._make_summary(order, failure_reason=msg)

        try:
            self._ensure_rakuten_session()
        except RakutenLoginError as e:
            msg = "乐天登录失败: %s" % e
            self.logger.error("乐天书店：%s order=%s", msg, order_id)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="乐天书店：请检查 login.password 或手动完成登录后重试。",
                )
            except Exception:
                pass
            return False, self._make_summary(order, failure_reason=msg)

        try:
            self._clear_cart(driver)
        except Exception as e:
            msg = "清空购物车失败: %s" % e
            self.logger.error("乐天书店：%s order=%s", msg, order_id)
            return False, self._make_summary(order, failure_reason=msg)

        for idx, product in enumerate(products, 1):
            purl = normalize_rakuten_books_product_url(
                str(product.get("url") or "").strip()
            )
            if not purl:
                continue
            source_lines: List[Dict[str, Any]] = list(
                product.get("_source_lines") or [product]
            )
            # 与乐天市场一致：先校验 List 行齐全，再浏览器加购，再按行逐条 addedCart
            for line in source_lines:
                gid, gno = self._api_goods_id_and_no(line)
                if not gid:
                    msg = (
                        "订单商品缺少 GoodsId（getOrderListSimple List） "
                        "goods_id=%r goods_no=%r url=%s"
                        % (gid, gno, str(line.get("url") or purl))
                    )
                    self.logger.error("乐天书店：%s order=%s", msg, order_id)
                    try:
                        self.feishu_notifier.notify_order_issue(
                            str(order_id),
                            [msg],
                            user_id=order.get("user_id"),
                            extra="乐天书店：接口 List 缺 GoodsId，已跳过本单。",
                        )
                    except Exception:
                        pass
                    return False, self._make_summary(order, failure_reason=msg)
                if not gno:
                    line_bid = extract_rakuten_book_id(
                        normalize_rakuten_books_product_url(
                            str(line.get("url") or purl)
                        )
                    )
                    if not line_bid:
                        msg = (
                            "订单商品缺少 GoodsNo（getOrderListSimple List） "
                            "goods_id=%r url=%s" % (gid, purl)
                        )
                        self.logger.error("乐天书店：%s order=%s", msg, order_id)
                        return False, self._make_summary(order, failure_reason=msg)

            # 浏览器：按接口行加购（每行用该行数量；不跨行合并）
            try:
                self.logger.info(
                    "乐天书店：加购组 %s/%s 接口行=%s %s",
                    idx,
                    len(products),
                    len(source_lines),
                    purl,
                )
                for line in source_lines:
                    line_qty = max(1, int(line.get("quantity") or 1))
                    line_url = normalize_rakuten_books_product_url(
                        str(line.get("url") or purl)
                    )
                    self._add_product_to_cart(driver, line_url or purl, line_qty)
            except Exception as e:
                msg = "加购失败: %s url=%s" % (e, purl)
                self.logger.error(msg)
                try:
                    self.feishu_notifier.notify_order_issue(
                        str(order_id), [msg], user_id=order.get("user_id"), extra="乐天书店"
                    )
                except Exception:
                    pass
                return False, self._make_summary(order, failure_reason=msg)

            # 后端：List 每一行单独 addedCartCallbackSimple（与乐天市场相同）
            for line in source_lines:
                line_url = normalize_rakuten_books_product_url(
                    str(line.get("url") or purl)
                )
                line_bid = extract_rakuten_book_id(line_url) or ""
                gid, gno = self._api_goods_id_and_no(line)
                if not gno:
                    gno = line_bid or gid
                line_qty = max(1, int(line.get("quantity") or 1))
                callback_product: Dict[str, Any] = {
                    "goods_id": gid,
                    "goods_no": gno,
                    "shop_id": self._store_name(order),
                    "quantity": line_qty,
                    "url": line_url,
                }
                self.logger.info(
                    "乐天书店：加购回调 GoodsId=%s GoodsNo=%s qty=%s book_id=%s Mark=%s",
                    gid,
                    gno,
                    line_qty,
                    line_bid or "",
                    str(order.get("mark") or "")[:24],
                )
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
                    return False, self._make_summary(
                        order, failure_reason="加购回调异常: %s" % e
                    )
                if not ok_cb:
                    tip = str(cb_msg or "")
                    msgs = [
                        "乐天书店：addedCartCallbackSimple 未成功"
                        "（GoodsId=%s GoodsNo=%s book=%s Message=%s）"
                        % (gid, gno, line_bid or line_url, tip or "-")
                    ]
                    self.logger.error("%s order=%s", msgs[0], order_id)
                    try:
                        self.ticket_creator.create_ticket(
                            str(order_id), msgs, user_id=order.get("user_id")
                        )
                    except Exception:
                        pass
                    try:
                        self.feishu_notifier.notify_order_issue(
                            str(order_id),
                            msgs,
                            user_id=order.get("user_id"),
                            extra="乐天书店加购回调失败",
                        )
                    except Exception:
                        pass
                    return False, self._make_summary(
                        order,
                        failure_reason="addedCartCallbackSimple 未成功: %s"
                        % (tip or "-"),
                    )

        cart_url = (self.rb_cfg.get("cart_url") or "").strip() or (
            "https://books.step.rakuten.co.jp/rms/mall/book/bs/Cart"
        )
        self._navigate(driver, cart_url.split("#")[0])
        time.sleep(float(self.rb_cfg.get("wait_after_cart_load_seconds", 2)))

        checkout_sel = (self.rb_cfg.get("checkout_button_css") or "button#js-cartBtn").strip()
        try:
            self._random_pre_click_wait("ご購入手続き")
            ck = WebDriverWait(driver, 25).until(EC.element_to_be_clickable((By.CSS_SELECTOR, checkout_sel)))
            driver.execute_script("arguments[0].click();", ck)
            time.sleep(float(self.rb_cfg.get("wait_after_checkout_seconds", 4)))
            self._ensure_session_after_nav(driver)
        except Exception as e:
            return False, self._make_summary(order, failure_reason="进入结算失败: %s" % e)

        # 偶发停在「支払いと配送」等中间页，需点「次へ」才到注文確認
        try:
            self._pass_books_checkout_intermediates(driver)
        except Exception as e:
            msg = "进入注文確認失败: %s" % e
            self.logger.error("乐天书店：%s", msg)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="乐天书店：购物车后未到达注文内容の確認页。",
                )
            except Exception:
                pass
            return False, self._make_summary(order, failure_reason=msg)

        goods_list: List[Dict[str, Any]] = []
        total = goods_fee = operate_fee = 0
        try:
            goods_list, total, goods_fee, operate_fee = (
                self._parse_goods_list_from_confirm_page(driver)
            )
        except Exception as e:
            self.logger.warning("乐天书店：解析结算页商品列表失败: %s", e)

        if not goods_list:
            for p in products:
                purl = (p.get("url") or "").strip()
                bid = extract_rakuten_book_id(purl)
                if not bid:
                    continue
                try:
                    price_int = int(round(float(p.get("price") or 0)))
                except Exception:
                    price_int = 0
                q = int(p.get("quantity") or 1)
                goods_list.append(
                    {
                        "No": bid,
                        "Num": max(1, q),
                        "StoreName": self._store_name(order),
                        "Price": price_int,
                    }
                )
            try:
                gf, of, tot = self._parse_books_confirm_totals(driver)
                goods_fee = gf or goods_fee
                operate_fee = of
                total = tot or total
            except Exception:
                pass
            if goods_fee <= 0:
                goods_fee = sum(int(x["Price"]) * int(x["Num"]) for x in goods_list)
            if total <= 0:
                total = goods_fee + operate_fee
            if total > 0 and goods_fee + operate_fee != total:
                operate_fee = total - goods_fee

        # checkCart 优先用接口 GoodsNo（市场转书店时确认页 DOM 常对不上）
        order_goods = self._goods_list_from_order_products(products, order)
        if order_goods:
            # 页面有单价时覆写价格，数量以订单为准（合并后更稳）
            if goods_list:
                page_by_idx = list(goods_list)
                for i, og in enumerate(order_goods):
                    if i < len(page_by_idx):
                        pp = int(page_by_idx[i].get("Price") or 0)
                        if pp > 0:
                            og["Price"] = pp
            # 若订单价仍为 0 而总价已知且仅一行，用总价摊
            if (
                len(order_goods) == 1
                and int(order_goods[0].get("Price") or 0) <= 0
                and goods_fee > 0
            ):
                order_goods[0]["Price"] = int(goods_fee) // max(
                    1, int(order_goods[0].get("Num") or 1)
                )
            goods_list = order_goods
            self.logger.info(
                "乐天书店：checkCart GoodsList 改用订单 GoodsNo store=%s n=%s sample=%s",
                self._store_name(order),
                len(goods_list),
                (goods_list[0].get("No") if goods_list else ""),
            )

        shot_path = None
        screen_url = ""
        ok_chk = False
        chk_err = ""
        chk_raw = ""
        try:
            try:
                shot_path = take_full_page_screenshot(driver)
                screen_url = upload_screenshot_get_url(shot_path, self.config) or ""
            except Exception as e:
                self.logger.warning(
                    "乐天书店：确认页截图/上传失败（不阻断下单）: %s", e
                )
                screen_url = ""
            if not screen_url:
                self.logger.warning(
                    "乐天书店：无 ScreenShotUrl，仍继续 checkCart / 点击确定"
                )

            self._ensure_callback_pc_mark(order)
            self._log_callback_identity("checkCart", order)
            self.logger.info(
                "乐天书店：调用 checkCartGoodsSimple Total=%s GoodsFee=%s OperateFee=%s goods=%s",
                total,
                goods_fee,
                operate_fee,
                len(goods_list or []),
            )
            ok_chk, chk_err, chk_raw = check_cart_goods_simple(
                order,
                total=total,
                goods_fee=goods_fee,
                operate_fee=operate_fee,
                screenshot_url=screen_url,
                config=self.config,
                goods_list_override=goods_list,
                use_curl=use_curl,
            )
            # 与乐天市场一致：不调 getOrderSimple 刷 Mark 重试
        finally:
            if shot_path:
                try:
                    import os

                    os.remove(shot_path)
                except Exception:
                    pass

        if not ok_chk:
            allow_continue = bool(
                self.rb_cfg.get("commit_even_if_check_cart_fails", False)
            )
            if allow_continue:
                # 市场→书店转交常见 Mark/状态不一致；继续下单，勿发「需人工」以免误判整单失败
                self.logger.warning(
                    "乐天书店：checkCart 失败仍继续点「注文を確定する」 err=%s",
                    chk_err,
                )
            else:
                try:
                    self.feishu_notifier.notify_order_issue(
                        str(order_id),
                        [chk_err or "checkCartGoodsSimple 失败"],
                        user_id=order.get("user_id"),
                        extra="乐天书店结算校验失败",
                    )
                except Exception:
                    pass
                return False, self._make_summary(
                    order,
                    failure_reason=chk_err or "checkCartGoodsSimple 失败",
                    check_cart_requested=True,
                    check_cart_response=(chk_raw or "")[:500],
                )

        commit_sel = (
            self.rb_cfg.get("commit_order_button_css")
            or "button.btn-red[name='commit_order'], button[name='commit_order'], "
            "form[action*='ConfirmOrderFork'] button[name='commit_order'], "
            ".js-float-box button[name='commit_order'], "
            ".area-cost-detail button[name='commit_order']"
        ).strip()
        try:
            # 截图/校验期间若会话失效，先自动登录再点确定
            self._ensure_session_after_nav(driver)
            if not self._is_books_confirm_page(driver):
                self._pass_books_checkout_intermediates(driver)
            self._click_books_commit_order(driver, commit_sel)
        except Exception as e:
            return False, self._make_summary(
                order,
                failure_reason="点击注文確定失败: %s" % e,
                check_cart_requested=True,
                check_cart_response="ok",
            )

        sec = int(self.rb_cfg.get("success_page_wait_seconds", 120))
        deadline = time.time() + max(10, sec)
        ok_page = False
        while time.time() < deadline:
            try:
                self._ensure_session_after_nav(driver)
            except Exception:
                pass
            if self._is_books_success_page(driver):
                ok_page = True
                break
            # 仍停在确认页：再点一次确定（偶发第一次未真正提交）
            if self._is_books_confirm_page(driver) and (deadline - time.time()) > 30:
                try:
                    self.logger.warning("乐天书店：等待完了页期间仍在确认页，再次点击确定")
                    self._click_books_commit_order(driver, commit_sel)
                except Exception as e:
                    self.logger.warning("乐天书店：再次点击确定失败: %s", e)
            time.sleep(1.5)

        if not ok_page:
            msg = "乐天书店：超时未检测到注文完了页"
            self.logger.error("%s URL=%s", msg, getattr(driver, "current_url", ""))
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg, driver.current_url or ""],
                    user_id=order.get("user_id"),
                    extra="将跳过本单并继续后续订单（非暂停）。",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=msg,
                check_cart_requested=True,
                check_cart_response="ok",
                runner_pause_requested=False,
            )

        purchase_no, detail_url = self._extract_books_order_info(driver)
        if not purchase_no:
            msg = "乐天书店：成功页未解析到注文番号"
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id), [msg], user_id=order.get("user_id"), extra="乐天书店"
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=msg,
                check_cart_requested=True,
                check_cart_response="ok",
            )

        if not detail_url:
            tpl = (self.rb_cfg.get("purchase_url_template") or "").strip() or (
                "https://books.rakuten.co.jp/mypage/delivery/status?order_number={purchase_no}"
            )
            detail_url = tpl.format(purchase_no=purchase_no)
        self.logger.info(
            "乐天书店：注文完了 purchase_no=%s detail=%s", purchase_no, detail_url
        )
        purchase_nobs = [{"no": purchase_no, "url": detail_url}]

        self._ensure_callback_pc_mark(order)
        self._log_callback_identity("addNo", order)
        self.logger.info(
            "乐天书店：调用 addNoCallbackSimple CreditCard=%s PurchaseNo=%s",
            self._credit_card_label(order),
            purchase_no,
        )
        ok_add, add_err, add_raw = send_add_no_callback(
            order,
            purchase_nobs,
            credit_card=self._credit_card_label(order),
            config=self.config,
            use_curl=use_curl,
        )
        self.logger.info(
            "乐天书店：addNoCallbackSimple ok=%s err=%s body=%s",
            ok_add,
            add_err or "",
            (add_raw or "")[:300],
        )
        if not ok_add:
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [add_err or "addNoCallbackSimple 失败"],
                    user_id=order.get("user_id"),
                    extra="乐天书店：页面已下单成功但完成回调失败，请人工核对后台。purchase_no=%s"
                    % purchase_no,
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=add_err or "addNoCallbackSimple 失败",
                check_cart_requested=True,
                check_cart_response="ok" if ok_chk else (chk_err or "checkCart失败"),
                add_no_requested=True,
                add_no_response=(add_raw or "")[:500],
            )

        shot2 = None
        update_errors: List[str] = []
        goods_no_list = [
            {
                "no": str(g.get("No") or ""),
                "price": int(g.get("Price") or 0),
                "num": int(g.get("Num") or 1),
            }
            for g in goods_list
            if str(g.get("No") or "").strip()
        ]
        if not goods_no_list:
            goods_no_list = [
                {"no": purchase_no, "price": total or goods_fee, "num": 1}
            ]

        detail_shot_url = ""
        use_curl_upload = bool(
            (self.config.get("order_api") or {}).get("use_curl_for_order_api", True)
        )
        try:
            # 成功截图：优先打开「ご注文内容の変更・確認」详情页（含 back_number）
            self._open_books_order_detail_for_screenshot(driver, detail_url)
            for up_try in range(1, 4):
                try:
                    if shot2:
                        try:
                            import os

                            os.remove(shot2)
                        except Exception:
                            pass
                        shot2 = None
                    shot2 = take_full_page_screenshot(driver)
                    detail_shot_url = (
                        upload_screenshot_get_url(
                            shot2,
                            self.config,
                            use_curl=use_curl_upload,
                            use_requests=not use_curl_upload,
                        )
                        or ""
                    )
                    if detail_shot_url:
                        self.logger.info(
                            "乐天书店：详情截图上传成功 try=%s url=%s",
                            up_try,
                            detail_shot_url[:120],
                        )
                        break
                    self.logger.warning(
                        "乐天书店：详情截图上传返回空 try=%s/%s", up_try, 3
                    )
                except Exception as e:
                    self.logger.warning(
                        "乐天书店：详情截图/上传异常 try=%s/%s: %s", up_try, 3, e
                    )
                    detail_shot_url = ""
                time.sleep(1.0)

            if not detail_shot_url:
                msg = "详情页截图上传失败（将仍提交 updateGoodsNoCallback，ScreenShotUrls 为空）"
                update_errors.append(msg)
                self.logger.error("乐天书店：%s", msg)

            self._ensure_callback_pc_mark(order)
            self._log_callback_identity("updateGoodsNo", order)
            self.logger.info(
                "乐天书店：调用 updateGoodsNoCallback PurchaseNo=%s shot=%s goods=%s StoreName=%s",
                purchase_no,
                "yes" if detail_shot_url else "no",
                len(goods_no_list),
                self._store_name(order),
            )
            ok_u, uerr = send_update_goods_no_callback(
                order,
                purchase_no,
                goods_no_list,
                detail_shot_url or "",
                self._store_name(order),
                self.config,
                use_curl=use_curl,
            )
            self.logger.info(
                "乐天书店：updateGoodsNoCallback ok=%s err=%s",
                ok_u,
                uerr or "",
            )
            if not ok_u:
                update_errors.append(uerr or "updateGoodsNoCallback 失败")
        except Exception as e:
            self.logger.exception("乐天书店：分单回调阶段异常: %s", e)
            update_errors.append(str(e))
        finally:
            if shot2:
                try:
                    import os

                    os.remove(shot2)
                except Exception:
                    pass

        if update_errors:
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    update_errors
                    + [
                        "purchase_no=%s" % purchase_no,
                        "页面已下单；请核对 addNo/updateGoodsNo/截图是否落库",
                    ],
                    user_id=order.get("user_id"),
                    extra="乐天书店分单回调异常",
                )
            except Exception:
                pass

        return True, self._make_summary(
            order,
            success=True,
            payment_method="rakuten_one_click",
            check_cart_requested=True,
            check_cart_response="ok" if ok_chk else (chk_err or "checkCart失败仍下单"),
            add_no_requested=True,
            add_no_response=(add_raw or "")[:500],
            update_errors=update_errors,
        )
