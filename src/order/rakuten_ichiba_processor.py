# -*- coding: utf-8 -*-
"""
乐天市场（item.rakuten.co.jp / cart.step.rakuten.co.jp）：
清空购物车 → 逐品加购（含 SKU 弹窗/备用按钮）→ 领券 → 购物车取金额并 checkCart → 店铺结算 → 注文確定 → 回调。
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

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

_BOOKS_RB_URL_RE = re.compile(r"books\.rakuten\.co\.jp/rb/", re.IGNORECASE)
_BOOKS_HOST_RE = re.compile(r"books\.rakuten\.co\.jp", re.IGNORECASE)
# 接口常给 item.rakuten.co.jp/book/...，打开后会跳到书店站
_BOOKS_ICHIBA_SHOP_RE = re.compile(
    r"item\.rakuten\.co\.jp/book(?:/|$)", re.IGNORECASE
)
# 联盟链接里斜杠常为 %2F：item.rakuten.co.jp%2Fbook%2F17959171
_BOOKS_ICHIBA_SHOP_ENCODED_RE = re.compile(
    r"item\.rakuten\.co\.jp(?:/|%2[Ff])book(?:/|%2[Ff]|$)", re.IGNORECASE
)
_ORDER_NO_RE = re.compile(r"(\d+)-(\d{8})-(\d{10})")
_ADD_OK_TEXT = "商品をかごに追加しました"


class RakutenBooksHandoffNeeded(Exception):
    """商品实为乐天书店，应转交书店流程（保留当前订单回调身份）。"""

    def __init__(self, url: str = ""):
        self.url = (url or "").strip()
        super().__init__(
            "商品已跳转乐天书店（books.rakuten.co.jp），需转交书店流程: %s" % self.url
        )


def _parse_yen_int(text: str) -> int:
    if not text:
        return 0
    s = re.sub(r"[^\d]", "", str(text).strip())
    return int(s) if s else 0


def _strip_phone_and_postal_noise(text: str) -> str:
    """去掉电话/邮编等，避免把 080-7535-8884 误当成金额 8884。"""
    raw = str(text or "")
    # 日式电话：080-7535-8884 / 03-1234-5678 / 0120-xxx-xxx
    raw = re.sub(r"\b0\d{1,4}-\d{1,4}-\d{3,4}\b", " ", raw)
    raw = re.sub(r"\b0\d{9,11}\b", " ", raw)
    # 邮编 〒550-0022
    raw = re.sub(r"〒?\s*\d{3}-\d{4}", " ", raw)
    return raw


def _extract_yen_candidates(text: str) -> List[int]:
    """
    从一行文案中提取金额候选。
    避免把「5点」「电话号」「邮编」误当成金额。
    优先识别带 円/¥/千分位 的金额。
    """
    raw = _strip_phone_and_postal_noise(text)
    found: List[int] = []
    explicit: List[int] = []

    # 明确货币：¥9,418 / 9,418円 / 9418円
    for m in re.finditer(
        r"[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)|([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*円",
        raw,
    ):
        g = m.group(1) or m.group(2) or ""
        try:
            v = int(g.replace(",", ""))
        except Exception:
            continue
        end = m.end()
        tail = raw[end : end + 2]
        if tail.startswith(("点", "個", "个")):
            continue
        explicit.append(v)

    if explicit:
        out: List[int] = []
        for v in explicit:
            if v not in out:
                out.append(v)
        return out

    # 退路：千分位数字（9,418）；避免匹配电话残留碎数字
    for m in re.finditer(r"\b([0-9]{1,3}(?:,[0-9]{3})+)\b", raw):
        try:
            v = int(m.group(1).replace(",", ""))
        except Exception:
            continue
        found.append(v)

    out = []
    for v in found:
        if v not in out:
            out.append(v)
    return out


def _pick_row_yen_amount(text: str, *, prefer_max: bool = True) -> int:
    cands = _extract_yen_candidates(text)
    if not cands:
        return 0
    if prefer_max:
        return max(cands)
    return cands[-1]


def _url_candidates_for_books_detect(url: str) -> List[str]:
    """原始 URL、解码 URL、联盟 pc/m 参数，用于识别书店商品。"""
    raw = str(url or "").strip()
    if not raw:
        return []
    out: List[str] = [raw]
    try:
        out.append(unquote(raw))
    except Exception:
        pass
    try:
        parsed = urlparse(raw)
        host = (parsed.netloc or "").lower()
        if "afl.rakuten.co.jp" in host:
            qs = parse_qs(parsed.query)
            for key in ("pc", "m"):
                val = (qs.get(key) or [""])[0].strip()
                if not val:
                    continue
                out.append(val)
                try:
                    out.append(unquote(val))
                except Exception:
                    pass
    except Exception:
        pass
    # 去重保序
    seen = set()
    uniq: List[str] = []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def is_rakuten_books_product_url(url: str) -> bool:
    """
    乐天书店商品：含 rb 直链、books 域名、市场站 book 店铺链，
    以及联盟跟踪链（hb.afl...?...pc=...item.../book/...，斜杠可能是 %2F）。
    """
    for u in _url_candidates_for_books_detect(url):
        if (
            _BOOKS_RB_URL_RE.search(u)
            or _BOOKS_HOST_RE.search(u)
            or _BOOKS_ICHIBA_SHOP_RE.search(u)
            or _BOOKS_ICHIBA_SHOP_ENCODED_RE.search(u)
        ):
            return True
    return False


class RakutenIchibaOrderProcessor(LoggerMixin):
    def __init__(self, config: Dict[str, Any], browser_manager: BrowserManager):
        self.config = config
        self.browser_manager = browser_manager
        self.ri_cfg = config.get("rakuten_ichiba") or {}
        self.ticket_creator = TicketCreator(config)
        self.feishu_notifier = FeishuNotifier(config)
        self.session_guard = (
            RakutenSessionGuard(browser_manager, config)
            if RakutenSessionGuard.is_enabled(config)
            else None
        )

    def _random_pre_click_wait(self, action: str) -> None:
        pay_cfg = self.config.get("payment") or {}
        rng = self.ri_cfg.get("pre_click_wait_seconds_range") or pay_cfg.get(
            "pre_click_wait_seconds_range", [0.7, 1.8]
        )
        try:
            mn = float(rng[0])
            mx = float(rng[1])
        except Exception:
            mn, mx = 0.7, 1.8
        sec = random.uniform(min(mn, mx), max(mn, mx))
        self.logger.info("乐天市场：关键点击前随机等待 %.2f 秒（%s）", sec, action)
        time.sleep(sec)

    def _store_name(self) -> str:
        return (self.ri_cfg.get("store_name") or "乐天市场").strip()

    def _credit_card_label(self) -> str:
        """addNoCallbackSimple 回传的 CreditCard（与后端约定标识，默认 8828；非浏览器选卡）。"""
        pay = self.config.get("payment") or {}
        return (
            (self.ri_cfg.get("add_no_credit_card") or pay.get("add_no_credit_card") or "8828")
        ).strip()

    @staticmethod
    def _line_no_for_check_cart(product: Dict[str, Any]) -> str:
        return (
            str(product.get("goods_no") or "").strip()
            or str(product.get("goods_id") or "").strip()
        )

    @staticmethod
    def _api_goods_id_and_no(product: Dict[str, Any]) -> Tuple[str, str]:
        gid = str(product.get("goods_id") or "").strip()
        gno = str(product.get("goods_no") or "").strip()
        return gid, gno

    def _make_summary(
        self,
        order: Dict[str, Any],
        *,
        success: bool = False,
        failure_reason: str = "",
        payment_method: str = "rakuten_creditcard",
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

    def _navigate(self, driver, url: str) -> None:
        target = url.split("#")[0]
        BrowserManager.navigate_allow_timeout(driver, target, self.logger)
        self._ensure_rakuten_session(resume_url=target)

    def _ensure_rakuten_session(self, resume_url=None) -> None:
        """若落在乐天统一登录域名，则按配置自动填密码继续。"""
        if not self.session_guard:
            return
        self.session_guard.ensure_logged_in(resume_url=resume_url)

    def _cart_url(self) -> str:
        return (self.ri_cfg.get("cart_url") or "https://cart.step.rakuten.co.jp/cart").strip()

    def _find_elements_now(self, driver, by: By, value: str):
        """探测可选元素时禁用隐式等待，避免每个不存在的选择器额外等待 10 秒。"""
        implicit_wait = float((self.config.get("browser") or {}).get("implicit_wait", 10))
        try:
            driver.implicitly_wait(0)
            return driver.find_elements(by, value)
        finally:
            driver.implicitly_wait(implicit_wait)

    def _find_elements_in(self, driver, root, by: By, value: str):
        """在 driver 或 WebElement 子树内查找，同样禁用隐式等待。"""
        implicit_wait = float((self.config.get("browser") or {}).get("implicit_wait", 10))
        try:
            driver.implicitly_wait(0)
            return root.find_elements(by, value)
        finally:
            driver.implicitly_wait(implicit_wait)

    @staticmethod
    def _is_on_cart_page(driver) -> bool:
        try:
            url = (driver.current_url or "").lower()
            # 仅认真正购物车域；basket.step 多为结算中间页，不能当成已在购物车
            return "cart.step.rakuten.co.jp" in url
        except Exception:
            return False

    def _ensure_cart_page(self, driver, *, quick: bool = False) -> None:
        """进入购物车；若已在购物车页则跳过整页导航。"""
        if self._is_on_cart_page(driver):
            if not quick:
                time.sleep(
                    float(self.ri_cfg.get("wait_after_cart_load_seconds", 2)) * 0.2
                )
            return
        self._navigate(driver, self._cart_url())
        base = float(self.ri_cfg.get("wait_after_cart_load_seconds", 2))
        time.sleep(min(base, 0.8) if quick else base)

    @staticmethod
    def _direct_product_url(url: str) -> str:
        """乐天跟踪链接优先取 pc 参数中的真实 item.rakuten.co.jp 地址。"""
        import html as _html
        from urllib.parse import unquote

        raw = _html.unescape((url or "").strip())
        try:
            parsed = urlparse(raw)
            if parsed.netloc.lower() == "hb.afl.rakuten.co.jp":
                pc = (parse_qs(parsed.query).get("pc") or [""])[0].strip()
                pc = _html.unescape(unquote(pc)).strip()
                if pc.startswith(("http://", "https://")):
                    return pc
        except Exception:
            pass
        return raw

    def _refresh_allow_timeout(self, driver) -> None:
        try:
            driver.refresh()
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            self.logger.warning("乐天市场：刷新页面超时，已停止加载并继续检查")
        except Exception as e:
            # 刷新时窗口已死：上抛给 clear_cart 做恢复重试
            raise

    def _cart_is_explicitly_empty(self, driver) -> bool:
        texts = self.ri_cfg.get("cart_empty_texts") or [
            "買い物かごに商品がありません",
            "買い物かごは空です",
            "現在、買い物かごには商品がありません",
            "買い物かごに商品は入っていません",
            "商品が入っていません",
        ]
        try:
            src = driver.page_source or ""
        except Exception:
            return False
        return any(str(text).strip() and str(text).strip() in src for text in texts)

    def _dismiss_interruptions(self, driver, timeout: float = 4.0) -> None:
        """关闭捐赠弹窗、通用 modal 等（失败不阻断）。"""
        deadline = time.time() + max(0.5, timeout)
        while time.time() < deadline:
            dismissed = False
            try:
                popup = self._find_elements_now(
                    driver, By.CSS_SELECTOR, "#give-freely-checkout-popup a[href='#']"
                )
                for el in popup:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        dismissed = True
                        time.sleep(0.3)
            except Exception:
                pass
            try:
                for el in self._find_elements_now(
                    driver,
                    By.CSS_SELECTOR, 'button[aria-label="モーダルを閉じる"]'
                ):
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        dismissed = True
                        time.sleep(0.3)
            except Exception:
                pass
            try:
                for el in self._find_elements_now(
                    driver,
                    By.CSS_SELECTOR, 'button[aria-label="ポップアップを閉じる"]'
                ):
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        dismissed = True
                        time.sleep(0.3)
            except Exception:
                pass
            if not dismissed:
                break
            time.sleep(0.25)

    def _iter_visible_modals(self, driver) -> List[Any]:
        """返回当前可见的确认页/通用 modal 根节点。"""
        roots: List[Any] = []
        seen = set()
        for css in ('[aria-modal="true"]', 'div[role="dialog"]'):
            for el in self._find_elements_now(driver, By.CSS_SELECTOR, css):
                try:
                    if id(el) in seen:
                        continue
                    seen.add(id(el))
                    aria_hidden = (el.get_attribute("aria-hidden") or "").strip().lower()
                    if aria_hidden == "true":
                        continue
                    if not el.is_displayed():
                        continue
                    roots.append(el)
                except Exception:
                    continue
        return roots

    def _click_first_option_in_modal(self, driver, modal) -> bool:
        """在弹窗内选择第一个 radio / 有效 select 选项。"""
        chose = False
        radios = self._find_elements_in(
            driver, modal, By.CSS_SELECTOR, 'input[type="radio"]'
        )
        visible_radios = []
        for radio in radios:
            try:
                if radio.is_displayed():
                    visible_radios.append(radio)
            except Exception:
                continue
        if visible_radios:
            first = visible_radios[0]
            try:
                if not first.is_selected():
                    driver.execute_script("arguments[0].click();", first)
                    time.sleep(0.25)
                chose = True
            except Exception:
                try:
                    label = first.find_element(By.XPATH, "./ancestor::label[1]")
                    driver.execute_script("arguments[0].click();", label)
                    time.sleep(0.25)
                    chose = True
                except Exception:
                    pass

        for select_el in self._find_elements_in(driver, modal, By.CSS_SELECTOR, "select"):
            try:
                if not select_el.is_displayed():
                    continue
                select = Select(select_el)
                valid = []
                for option in select.options:
                    text = (option.text or "").strip()
                    value = (option.get_attribute("value") or "").strip()
                    if not text or text in ("選択してください", "選択"):
                        continue
                    if value == "" and text in ("選択してください", "選択"):
                        continue
                    valid.append((value, text))
                if not valid:
                    continue
                current = (select.first_selected_option.text or "").strip()
                if current in ("選択してください", "選択", ""):
                    value, text = valid[0]
                    if value:
                        select.select_by_value(value)
                    else:
                        select.select_by_visible_text(text)
                    time.sleep(0.25)
                    chose = True
            except Exception:
                continue
        return chose

    def _find_modal_confirm_button(self, driver, modal):
        """查找弹窗内「确认」类按钮（不含取消/关闭/变更）。"""
        exact_labels = (
            "確認する",
            "確認",
            "決定する",
            "決定",
            "同意する",
            "了承しました",
            "OK",
        )
        for label in exact_labels:
            for el in self._find_elements_in(
                driver, modal, By.CSS_SELECTOR, 'button[aria-label="%s"]' % label
            ):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except Exception:
                    continue
        for el in self._find_elements_in(driver, modal, By.CSS_SELECTOR, "button"):
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                aria = (el.get_attribute("aria-label") or "").strip()
                text = (el.text or "").strip()
                # 排除取消/关闭/变更等
                skip_tokens = (
                    "キャンセル",
                    "閉じる",
                    "変更",
                    "戻る",
                    "解除",
                    "さらに表示",
                )
                blob = "%s %s" % (aria, text)
                if any(tok in blob for tok in skip_tokens):
                    continue
                if aria in exact_labels or text in exact_labels:
                    return el
                # 文案恰好为「確認」或以其开头的短按钮
                if text in exact_labels or aria.split()[:1] == ["確認"]:
                    return el
            except Exception:
                continue
        return None

    def _handle_confirm_page_option_modal(
        self, driver, timeout: float = 6.0
    ) -> bool:
        """
        确认订单页弹窗：选择第一个选项后点确认。
        典型出现在点击「注文を確定する」前后（店铺注意事项 / 配送选项等）。
        """
        deadline = time.time() + max(0.5, float(timeout))
        handled_any = False
        idle_rounds = 0
        while time.time() < deadline:
            modals = self._iter_visible_modals(driver)
            if not modals:
                if handled_any:
                    return True
                idle_rounds += 1
                if idle_rounds >= 3:
                    return handled_any
                time.sleep(0.2)
                continue
            idle_rounds = 0
            progressed = False
            for modal in modals:
                confirm_btn = self._find_modal_confirm_button(driver, modal)
                if confirm_btn is None:
                    continue
                # 有确认按钮时：先选第一项（若有），再点确认
                self._click_first_option_in_modal(driver, modal)
                try:
                    driver.execute_script("arguments[0].click();", confirm_btn)
                    handled_any = True
                    progressed = True
                    self.logger.info("乐天市场：已处理确认页弹窗（选第一项并确认）")
                    time.sleep(0.6)
                    break
                except Exception as e:
                    self.logger.warning("乐天市场：确认页弹窗点击失败: %s", e)
            if not progressed:
                time.sleep(0.25)
        return handled_any

    def _clear_cart(self, driver) -> None:
        """逐条删除；每次等待原按钮失效并刷新，禁止连续点击同一个旧按钮。"""
        def _is_window_dead_error(err: Exception) -> bool:
            msg = str(err or "").lower()
            return any(
                tok in msg
                for tok in (
                    "no such window",
                    "web view not found",
                    "target window already closed",
                    "invalid session id",
                    "chrome not reachable",
                )
            )

        try:
            self._navigate(driver, self._cart_url())
        except Exception as e:
            if _is_window_dead_error(e):
                self.logger.warning(
                    "乐天市场：打开购物车时窗口失效，尝试恢复浏览器后重试: %s", e
                )
                driver = self.browser_manager.ensure_alive(restart_if_dead=True)
                self._navigate(driver, self._cart_url())
            else:
                raise

        time.sleep(float(self.ri_cfg.get("wait_after_cart_load_seconds", 2)))
        self._dismiss_interruptions(driver)
        delete_sel = (self.ri_cfg.get("delete_item_button_css") or 'button[aria-label="削除する"]').strip()
        max_rounds = int(self.ri_cfg.get("clear_cart_max_delete_rounds", 30))
        no_button_checks = 0
        for rnd in range(max_rounds):
            try:
                btns = self._find_elements_now(driver, By.CSS_SELECTOR, delete_sel)
            except Exception as e:
                if _is_window_dead_error(e):
                    self.logger.warning(
                        "乐天市场：清车过程窗口失效，恢复后重开购物车: %s", e
                    )
                    driver = self.browser_manager.ensure_alive(restart_if_dead=True)
                    self._navigate(driver, self._cart_url())
                    time.sleep(
                        float(self.ri_cfg.get("wait_after_cart_load_seconds", 2))
                    )
                    self._dismiss_interruptions(driver)
                    no_button_checks = 0
                    continue
                btns = []
            visible = []
            for b in btns:
                try:
                    if b.is_displayed():
                        visible.append(b)
                except Exception:
                    continue
            if not visible:
                if self._cart_is_explicitly_empty(driver):
                    self.logger.info("乐天市场：已确认购物车为空")
                    return
                no_button_checks += 1
                if no_button_checks >= 3:
                    raise RuntimeError(
                        "购物车未找到删除按钮，也未检测到明确空购物车文案；"
                        "可能页面未完成加载、登录失效或页面结构变化"
                    )
                self.logger.info(
                    "乐天市场：暂未找到商品/空购物车标志，刷新后复查 %s/3",
                    no_button_checks,
                )
                try:
                    self._refresh_allow_timeout(driver)
                except Exception as e:
                    if _is_window_dead_error(e):
                        self.logger.warning(
                            "乐天市场：刷新购物车时窗口失效，恢复后重开: %s", e
                        )
                        driver = self.browser_manager.ensure_alive(
                            restart_if_dead=True
                        )
                        self._navigate(driver, self._cart_url())
                        time.sleep(
                            float(self.ri_cfg.get("wait_after_cart_load_seconds", 2))
                        )
                        self._dismiss_interruptions(driver)
                        no_button_checks = 0
                        continue
                    raise
                time.sleep(float(self.ri_cfg.get("wait_after_cart_refresh_seconds", 2)))
                self._dismiss_interruptions(driver, timeout=1.0)
                continue

            no_button_checks = 0
            btn = visible[0]
            before_count = len(visible)
            self.logger.info(
                "乐天市场：删除购物车商品（第 %s 轮，当前 %s 个删除按钮）",
                rnd + 1,
                before_count,
            )
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception as e:
                raise RuntimeError("点击削除する失败: %s" % e) from e
            try:
                WebDriverWait(
                    driver,
                    float(self.ri_cfg.get("delete_item_complete_wait_seconds", 15)),
                    poll_frequency=0.4,
                ).until(EC.staleness_of(btn))
            except Exception:
                self.logger.warning("乐天市场：删除后商品行未及时失效，将刷新页面确认")

            self._refresh_allow_timeout(driver)
            time.sleep(float(self.ri_cfg.get("wait_after_cart_refresh_seconds", 2)))
            self._dismiss_interruptions(driver, timeout=1.0)
        raise RuntimeError("清空购物车超过最大删除轮次 %s" % max_rounds)

    @staticmethod
    def _read_control_quantity(el) -> int:
        """读取数量控件当前值；失败返回 0。"""
        try:
            tag = (el.tag_name or "").lower()
            if tag == "select":
                raw = (el.get_attribute("value") or "").strip()
            else:
                raw = (el.get_attribute("value") or "").strip()
            m = re.match(r"(\d+)", raw or "")
            return int(m.group(1)) if m else 0
        except Exception:
            return 0

    def _apply_quantity_to_control(
        self, driver, el, quantity: int, *, allow_partial: bool = True
    ) -> bool:
        """
        设置数量控件。tel/input 必须用 React native value setter，
        直接赋值会出现「页面显示已改、购物车仍为 1」的假成功。
        select 若最大选项 < 目标值，则设为可选最大值（剩余由逐件补加兜底）。
        allow_partial：详情页可为 True；购物车改数必须 False（避免读回 1 却报设为 4）。
        """
        qty = max(1, min(100, int(quantity or 1)))
        tag = (el.tag_name or "").lower()
        try:
            if tag == "select":
                select = Select(el)
                numeric_values: List[int] = []
                for option in select.options:
                    raw = (option.get_attribute("value") or option.text or "").strip()
                    if re.fullmatch(r"\d+\+?", raw):
                        numeric_values.append(int(raw.rstrip("+")))
                if not numeric_values:
                    return False
                apply_qty = qty
                if qty not in numeric_values:
                    capped = max(
                        (v for v in numeric_values if v <= qty), default=0
                    )
                    if capped < 1:
                        return False
                    if capped < qty:
                        self.logger.info(
                            "乐天市场：数量 select 最大可选 %s，目标 %s，先设为 %s",
                            max(numeric_values),
                            qty,
                            capped,
                        )
                    apply_qty = capped
                try:
                    select.select_by_value(str(apply_qty))
                except Exception:
                    matched = False
                    for option in select.options:
                        raw = (option.get_attribute("value") or option.text or "").strip()
                        if re.fullmatch(r"\d+\+?", raw) and int(raw.rstrip("+")) == apply_qty:
                            select.select_by_value(option.get_attribute("value"))
                            matched = True
                            break
                    if not matched:
                        return False
                driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                    el,
                )
            else:
                # React 受控组件：必须走 HTMLInputElement.prototype.value setter + InputEvent
                ok = driver.execute_script(
                    """
                    const el = arguments[0];
                    const next = String(arguments[1]);
                    const proto = window.HTMLInputElement
                        ? window.HTMLInputElement.prototype
                        : null;
                    const desc = proto
                        ? Object.getOwnPropertyDescriptor(proto, 'value')
                        : null;
                    el.focus();
                    if (desc && desc.set) {
                        desc.set.call(el, next);
                    } else {
                        el.value = next;
                    }
                    el.dispatchEvent(new InputEvent('input', {
                        bubbles: true,
                        cancelable: true,
                        data: next,
                        inputType: 'insertText'
                    }));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.blur();
                    return String(el.value || '') === next;
                    """,
                    el,
                    str(qty),
                )
                if not ok:
                    return False
            time.sleep(0.35)
            actual = self._read_control_quantity(el)
            if actual <= 0:
                return False
            if actual == qty:
                return True
            # 详情页：允许被上限截断后由逐件补加凑齐；购物车改数必须精确命中
            if allow_partial and tag == "select" and actual < qty:
                return True
            return False
        except Exception:
            return False

    def _try_set_quantity_via_stepper(self, driver, root, quantity: int) -> bool:
        """步进器兜底：仅在 select / React input 均失败时使用。"""
        qty = max(1, min(100, int(quantity or 1)))
        if qty <= 1:
            return True
        try:
            qty_roots = []
            for css in (
                'div[irc="Quantity"]',
                "div.stepper--3h7VX",
            ):
                qty_roots.extend(
                    self._find_elements_in(driver, root, By.CSS_SELECTOR, css)
                )
            for qty_root in qty_roots:
                try:
                    if not qty_root.is_displayed():
                        continue
                except Exception:
                    continue
                inputs = self._find_elements_in(
                    driver,
                    qty_root,
                    By.CSS_SELECTOR,
                    'input[type="tel"], input[type="number"]',
                )
                if not inputs:
                    continue
                control = inputs[0]
                current = self._read_control_quantity(control) or 1
                if current == qty:
                    return True
                plus = None
                minus = None
                for a in self._find_elements_in(
                    driver, qty_root, By.CSS_SELECTOR, "a[class*='incrementor']"
                ):
                    cls = (a.get_attribute("class") or "").lower()
                    if "plus" in cls:
                        plus = a
                    elif "minus" in cls:
                        minus = a
                if current < qty and plus is not None:
                    for _ in range(qty - current):
                        try:
                            if "disabled" in ((plus.get_attribute("class") or "").lower()):
                                break
                            driver.execute_script("arguments[0].click();", plus)
                            time.sleep(0.08)
                        except Exception:
                            break
                elif current > qty and minus is not None:
                    for _ in range(current - qty):
                        try:
                            if "disabled" in ((minus.get_attribute("class") or "").lower()):
                                break
                            driver.execute_script("arguments[0].click();", minus)
                            time.sleep(0.08)
                        except Exception:
                            break
                time.sleep(0.25)
                if self._read_control_quantity(control) == qty:
                    return True
            return False
        except Exception:
            return False

    def _quantity_control_candidates(
        self, driver, root, scope: str
    ) -> List[Any]:
        """按页面区域收集数量控件；select 优先于 tel input（已验证有效路径）。"""
        cfg_key = {
            "pdp": "quantity_input_css",
            "dialog": "quantity_dialog_css",
            "cart": "quantity_cart_css",
        }.get(scope, "quantity_input_css")
        defaults = {
            "pdp": (
                'div[irc="Quantity"] select,'
                'div[irc="Quantity"] input[type="tel"],'
                'div.quantity--qnq4Z select,'
                'div.quantity--qnq4Z input[type="tel"],'
                'div.stepper--3h7VX select,'
                'div.stepper--3h7VX input[type="tel"]'
            ),
            "dialog": (
                'div[role="dialog"] div[irc="Quantity"] select,'
                'div[role="dialog"] div[irc="Quantity"] input[type="tel"],'
                'div[role="dialog"] div.quantity--qnq4Z select,'
                '[aria-modal="true"] div[irc="Quantity"] select,'
                '[aria-modal="true"] div[irc="Quantity"] input[type="tel"]'
            ),
            "cart": 'select.select--3Nrso, select[class*="select--"], input[type="tel"]',
        }
        default = defaults.get(scope) or defaults["pdp"]
        css_list = str(self.ri_cfg.get(cfg_key) or default).split(",")
        seen = set()
        controls: List[Any] = []
        for css in css_list:
            css = css.strip()
            if not css or css in seen:
                continue
            seen.add(css)
            for el in self._find_elements_in(driver, root, By.CSS_SELECTOR, css):
                if el in controls:
                    continue
                try:
                    if not el.is_displayed():
                        continue
                except Exception:
                    pass
                controls.append(el)
        return controls

    def _try_set_quantity_with_controls(
        self,
        driver,
        root,
        quantity: int,
        *,
        scope: str,
        label: str = "",
    ) -> bool:
        """主路径：select → React native input；步进器仅最后兜底。

        scope: 控件区域键（pdp / dialog / cart），不可用中文展示文案。
        label: 仅用于日志。
        """
        qty = max(1, min(100, int(quantity or 1)))
        if qty <= 1:
            return True
        log_label = (label or scope or "数量").strip()
        candidates = self._quantity_control_candidates(driver, root, scope)
        selects = [
            el for el in candidates if (el.tag_name or "").lower() == "select"
        ]
        inputs = [
            el
            for el in candidates
            if (el.tag_name or "").lower() != "select"
        ]
        for el in selects:
            if self._apply_quantity_to_control(driver, el, qty):
                actual = self._read_control_quantity(el)
                self.logger.info(
                    "乐天市场：%s数量已设为 %s（select，读回=%s）",
                    log_label,
                    qty,
                    actual,
                )
                return True
        for el in inputs:
            if self._apply_quantity_to_control(driver, el, qty):
                actual = self._read_control_quantity(el)
                self.logger.info(
                    "乐天市场：%s数量已设为 %s（React input，读回=%s）",
                    log_label,
                    qty,
                    actual,
                )
                return True
        if self._try_set_quantity_via_stepper(driver, root, qty):
            self.logger.info(
                "乐天市场：%s数量已通过步进器兜底设为 %s", log_label, qty
            )
            return True
        return False

    def _try_set_quantity_on_pdp(self, driver, quantity: int) -> bool:
        qty = max(1, min(100, int(quantity or 1)))
        if qty <= 1:
            return True
        if self._try_set_quantity_with_controls(
            driver, driver, qty, scope="pdp", label="详情页"
        ):
            return True
        self.logger.info(
            "乐天市场：详情页未找到数量控件，qty=%s（将依赖购物车/逐件补加）", qty
        )
        return False

    def _try_set_quantity_in_dialog(self, driver, quantity: int) -> bool:
        qty = max(1, min(100, int(quantity or 1)))
        if qty <= 1:
            return False
        for container_sel in ('div[role="dialog"]', '[aria-modal="true"]'):
            containers = self._find_elements_now(driver, By.CSS_SELECTOR, container_sel)
            for container in containers:
                try:
                    if not container.is_displayed():
                        continue
                except Exception:
                    continue
                if self._try_set_quantity_with_controls(
                    driver,
                    container,
                    qty,
                    scope="dialog",
                    label="加购弹窗",
                ):
                    return True
        return False

    def _find_cart_row_for_key(self, driver, product_key: str):
        path = product_key.split("?")[0].strip().lower()
        item_id = self._cart_key_item_id(product_key)
        search_tokens = []
        if path:
            search_tokens.append(path)
        if item_id and item_id not in search_tokens:
            search_tokens.append(item_id)
        for token in search_tokens:
            if not token:
                continue
            anchors = self._find_elements_now(
                driver,
                By.XPATH,
                "//a[contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz'), %r)]" % token,
            )
            for anchor in anchors:
                try:
                    rows = self._find_elements_in(
                        driver,
                        anchor,
                        By.XPATH,
                        "./ancestor::*[.//button[@aria-label='削除する']][1]",
                    )
                    if rows:
                        return rows[0]
                except Exception:
                    continue
        return None

    def _wait_cart_update_settled(self, driver, timeout: float = 12.0) -> None:
        """购物车改数量后会暂时禁用「購入手続き」，等其恢复再核验。"""
        deadline = time.time() + max(1.0, float(timeout))
        saw_busy = False
        while time.time() < deadline:
            try:
                btns = self._find_elements_now(
                    driver, By.CSS_SELECTOR, 'button[aria-label="購入手続き"]'
                )
                if not btns:
                    time.sleep(0.25)
                    continue
                btn = btns[0]
                disabled = bool(btn.get_attribute("disabled"))
                aria_dis = (btn.get_attribute("aria-disabled") or "").lower()
                busy = disabled or aria_dis in ("true", "1")
                if busy:
                    saw_busy = True
                    time.sleep(0.3)
                    continue
                # 见到过禁用再恢复，或一直可用都可结束
                if saw_busy or time.time() + 0.5 >= deadline:
                    return
                time.sleep(0.25)
            except Exception:
                time.sleep(0.25)
        # 超时不抛错，交由后续数量核验判定

    def _find_cart_quantity_select(self, driver, row):
        """
        购物车数量控件（实测稳定结构）：
          <div class="container--IfXHk">
            <div class="prefix-text--...">数量：</div>
            <select class="select--3Nrso ..."><option value="1">1</option>...</select>
          </div>
        需与同页都道府县 select（同 class）区分。
        """
        # 1) 优先：同容器内带「数量」前缀的 select
        for sel in self._find_elements_in(
            driver, row, By.CSS_SELECTOR, "select.select--3Nrso, select[class*='select--']"
        ):
            try:
                if not sel.is_displayed():
                    continue
            except Exception:
                continue
            try:
                parent = sel.find_element(By.XPATH, "./..")
                parent_text = (parent.text or "").strip()
            except Exception:
                parent_text = ""
            if "数量" not in parent_text:
                continue
            option_values = []
            try:
                for option in Select(sel).options:
                    raw = (option.get_attribute("value") or option.text or "").strip()
                    option_values.append(raw)
            except Exception:
                continue
            numeric_opts = [v for v in option_values if re.fullmatch(r"\d+\+?", v)]
            if len(numeric_opts) >= 2 and "1" in {v.rstrip("+") for v in numeric_opts}:
                return sel

        # 2) 兜底：沿用候选过滤（排除都道府县等）
        for el in self._quantity_control_candidates(driver, row, "cart"):
            if (el.tag_name or "").lower() != "select":
                continue
            option_values = []
            for option in Select(el).options:
                raw = (option.get_attribute("value") or option.text or "").strip()
                option_values.append(raw)
            if not option_values or option_values[0] == "":
                continue
            numeric_opts = [v for v in option_values if re.fullmatch(r"\d+\+?", v)]
            if len(numeric_opts) < 2 or "1" not in {v.rstrip("+") for v in numeric_opts}:
                continue
            if len(numeric_opts) < len(option_values) * 0.9:
                continue
            return el
        return None

    def _try_set_quantity_in_cart(self, driver, product_key: str, quantity: int) -> bool:
        qty = max(1, min(100, int(quantity or 1)))
        self._ensure_cart_page(driver, quick=True)
        self._dismiss_interruptions(driver, timeout=0.8)
        row = self._find_cart_row_for_key(driver, product_key)
        if row is None:
            self.logger.info("乐天市场：购物车未找到商品行 key=%s", product_key)
            return False
        el = self._find_cart_quantity_select(driver, row)
        if el is None:
            self.logger.info("乐天市场：购物车行无可用数量控件 key=%s", product_key)
            return False
        if not self._apply_quantity_to_control(
            driver, el, qty, allow_partial=False
        ):
            self.logger.warning(
                "乐天市场：购物车数量 select 写入失败，目标=%s key=%s", qty, product_key
            )
            return False

        # 改数后页面会短暂禁用「購入手続き」，需等结算恢复
        self._wait_cart_update_settled(
            driver,
            timeout=float(self.ri_cfg.get("wait_after_cart_qty_change_seconds", 12)),
        )
        # __INITIAL_STATE__ 不会在客户端改数后即时更新；刷新后再读 state
        try:
            self._refresh_allow_timeout(driver)
            time.sleep(1.0)
            self._dismiss_interruptions(driver, timeout=0.8)
        except Exception:
            pass

        actual_cart = self._get_cart_quantity_for_key(driver, product_key)
        if actual_cart == qty:
            self.logger.info(
                "乐天市场：购物车数量已设为 %s（key=%s，已刷新核验）", qty, product_key
            )
            return True
        self.logger.warning(
            "乐天市场：购物车数量写入后核验失败，目标=%s 实际=%s key=%s",
            qty,
            actual_cart,
            product_key,
        )
        return False

    def _get_cart_quantity_for_key(self, driver, product_key: str) -> int:
        self._ensure_cart_page(driver, quick=True)
        actual = self._actual_cart_quantities(driver)
        if not actual:
            return 0
        return self._lookup_cart_quantity(actual, product_key)

    def _set_quantity_on_pdp(self, driver, quantity: int) -> None:
        """兼容旧调用：失败时不抛错，由后续购物车/逐件补加兜底。"""
        self._try_set_quantity_on_pdp(driver, quantity)

    @staticmethod
    def _button_label(el) -> str:
        try:
            return (
                (el.get_attribute("aria-label") or "")
                + " "
                + (el.text or "")
            ).strip()
        except Exception:
            return ""

    @staticmethod
    def _is_cart_add_label(label: str) -> bool:
        return any(
            tok in (label or "")
            for tok in ("かごに追加", "カートに入れる", "カートに追加")
        )

    @staticmethod
    def _is_purchase_as_add_label(label: str) -> bool:
        """详情页「購入手続きへ」在部分店铺等价于加购（无单独かご按钮时）。"""
        text = label or ""
        if "購入手続き" not in text:
            return False
        # 购物车页结算按钮通常只有「購入手続き」，详情加购等价按钮多为「購入手続きへ」
        return "へ" in text or "購入手続きへ" in text

    def _find_add_to_cart_button(self, driver, prefer_fixed: bool):
        """
        查找加购按钮。
        优先「かごに追加」；若无，则「購入手続きへ」等价于加购。
        标准店铺：button[aria-label=...] + irc 容器。
        楽天24 等子站：只有按钮文案、无 aria-label / irc。
        """
        css_list: List[str] = []
        if prefer_fixed:
            css_list = [
                (self.ri_cfg.get("add_to_cart_fixed_css") or "").strip(),
                '#AddToCartPurchaseButtonFixed button[aria-label="かごに追加"]',
                '[irc="AddToCartPurchaseButtonFixed"] button[aria-label="かごに追加"]',
                '#AddToCartPurchaseButtonFixed button[aria-label*="購入手続き"]',
                '[irc="AddToCartPurchaseButtonFixed"] button[aria-label*="購入手続き"]',
                '#AddToCartPurchaseButtonFixed button',
                '[irc="AddToCartPurchaseButtonFixed"] button',
            ]
        else:
            css_list = [
                (self.ri_cfg.get("add_to_cart_floating_css") or "").strip(),
                '[irc="AddToCartPurchaseButtonFloating"] button[aria-label="かごに追加"]',
                '#floatingCartContainer button[aria-label="かごに追加"]',
                '[irc="AddToCartPurchaseButtonFloating"] button[aria-label*="購入手続き"]',
                '#floatingCartContainer button[aria-label*="購入手続き"]',
                '[irc="AddToCartPurchaseButtonFloating"] button',
                '#floatingCartContainer button',
            ]
        purchase_btn = None
        seen_css = set()
        for css in css_list:
            if not css or css in seen_css:
                continue
            seen_css.add(css)
            try:
                for el in self._find_elements_now(driver, By.CSS_SELECTOR, css):
                    try:
                        if not (el.is_displayed() and el.is_enabled()):
                            continue
                    except Exception:
                        continue
                    label = self._button_label(el)
                    if self._is_cart_add_label(label):
                        return el
                    if css.endswith('aria-label="かごに追加"]'):
                        return el
                    if self._is_purchase_as_add_label(label) and purchase_btn is None:
                        purchase_btn = el
            except Exception:
                continue

        # 文案兜底：覆盖楽天24 / fast-delivery-mart 等无 irc 页面
        xpaths_cart = [
            "//button[@aria-label='かごに追加']",
            "//button[contains(normalize-space(.), 'かごに追加')]",
            "//button[contains(normalize-space(.), 'カートに入れる')]",
            "//button[contains(normalize-space(.), 'カートに追加')]",
            "//a[contains(normalize-space(.), 'かごに追加')]",
        ]
        for xp in xpaths_cart:
            try:
                for el in self._find_elements_now(driver, By.XPATH, xp):
                    try:
                        if el.is_displayed() and el.is_enabled():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue

        if purchase_btn is not None:
            return purchase_btn

        xpaths_buy = [
            "//button[contains(@aria-label,'購入手続きへ')]",
            "//button[contains(normalize-space(.), '購入手続きへ')]",
            "//a[contains(normalize-space(.), '購入手続きへ')]",
        ]
        for xp in xpaths_buy:
            try:
                for el in self._find_elements_now(driver, By.XPATH, xp):
                    try:
                        if not (el.is_displayed() and el.is_enabled()):
                            continue
                    except Exception:
                        continue
                    # 避开购物车页结算按钮（通常在 cart.step 域名）
                    if self._is_on_cart_page(driver):
                        continue
                    if self._is_purchase_as_add_label(self._button_label(el)):
                        return el
            except Exception:
                continue
        return None

    def _confirm_sku_modal_if_needed(self, driver) -> None:
        """仅在真正 dialog/aria-modal 容器内确认；加购成功文案出现则立即返回。"""
        wait_sec = float(self.ri_cfg.get("sku_modal_wait_seconds", 2))
        selectors = [
            'div[role="dialog"] button[aria-label="購入手続きへ"]',
            '[aria-modal="true"] button[aria-label="購入手続きへ"]',
        ]
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            try:
                if driver.execute_script(
                    "return (document.body && document.body.innerText || '')"
                    ".indexOf(arguments[0]) >= 0;",
                    _ADD_OK_TEXT,
                ):
                    return
            except Exception:
                pass
            for sel in selectors:
                try:
                    for el in self._find_elements_now(driver, By.CSS_SELECTOR, sel):
                        if not el.is_displayed():
                            continue
                        self._random_pre_click_wait("SKU弹窗購入手続きへ")
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(0.4)
                        return
                except Exception:
                    continue
            time.sleep(0.2)

    def _click_add_to_cart_once(self, driver, prefer_fixed: bool) -> None:
        btn = self._find_add_to_cart_button(driver, prefer_fixed=prefer_fixed)
        if btn is None:
            raise RuntimeError("未找到可点击的加购按钮（かごに追加 / 購入手続きへ）")
        label = self._button_label(btn)
        action = (
            "購入手続きへ(加购)"
            if self._is_purchase_as_add_label(label)
            else "かごに追加"
        )
        self.logger.info("乐天市场：点击 %s", action)
        self._random_pre_click_wait(action)
        try:
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            btn.click()

    @staticmethod
    def _product_cart_identifiers(product: Dict[str, Any]) -> Tuple[str, str]:
        direct_url = RakutenIchibaOrderProcessor._direct_product_url(
            str(product.get("url") or "")
        )
        try:
            path = urlparse(direct_url).path.rstrip("/").lower()
        except Exception:
            path = ""
        name = str(product.get("name") or "").strip()
        return path, name

    @staticmethod
    def _product_cart_key(url: str) -> str:
        """购物车核验键：商品路径 + variantId（规格不同必须分开统计）。"""
        direct_url = RakutenIchibaOrderProcessor._direct_product_url(url)
        try:
            parsed = urlparse(direct_url)
            path = parsed.path.rstrip("/").lower()
            variant_id = (
                (parse_qs(parsed.query).get("variantId") or [""])[0].strip()
                or (parse_qs(parsed.query).get("variantid") or [""])[0].strip()
            )
            return "%s?variantId=%s" % (path, variant_id) if variant_id else path
        except Exception:
            return ""

    @staticmethod
    def _cart_key_item_id(key: str) -> str:
        """从核验键取商品 ID（路径最后一段），用于跳转子站后路径变化时的模糊匹配。"""
        base = (key or "").split("?")[0].rstrip("/").lower()
        if not base:
            return ""
        return base.rsplit("/", 1)[-1]

    @staticmethod
    def _cart_key_variant_id(key: str) -> str:
        raw = (key or "").lower()
        if "variantid=" not in raw:
            return ""
        try:
            # key 形如 /shop/item?variantId=xxx
            q = raw.split("?", 1)[1]
            return (parse_qs(q).get("variantid") or [""])[0].strip()
        except Exception:
            return ""

    def _lookup_cart_quantity(self, actual: Dict[str, int], product_key: str) -> int:
        """精确键优先；否则按商品 ID（+variantId）模糊匹配（楽天24 跳转等）。"""
        if not actual or not product_key:
            return 0
        if product_key in actual:
            return int(actual.get(product_key) or 0)
        item_id = self._cart_key_item_id(product_key)
        if not item_id:
            return 0
        want_variant = self._cart_key_variant_id(product_key)
        total = 0
        hit = False
        for key, qty in actual.items():
            if self._cart_key_item_id(key) != item_id:
                continue
            got_variant = self._cart_key_variant_id(key)
            if want_variant and got_variant and want_variant != got_variant:
                continue
            total += int(qty or 0)
            hit = True
        return total if hit else 0

    def _cart_quantity_maps_equal(
        self, expected: Dict[str, int], actual: Optional[Dict[str, int]]
    ) -> bool:
        """
        比较期望与购物车实际数量。
        接口 URL 常无 variantId，加购后购物车键却带 ?variantId=…（如选中 BLUE），
        此时精确字典不相等，但应按商品路径/ID 匹配（期望无 variant 时接受实际唯一/合计规格）。
        """
        if actual is None:
            return False
        if expected == actual:
            return True
        for key, qty in expected.items():
            if self._lookup_cart_quantity(actual, key) != int(qty or 0):
                return False
        # 总量一致，避免漏检购物车里多出来的其它 SKU
        if sum(int(v or 0) for v in expected.values()) != sum(
            int(v or 0) for v in actual.values()
        ):
            return False
        return True

    def _merge_duplicate_products(
        self, products: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        按商品路径+variantId 合并接口重复行，数量相加。
        例如 5 条 A×1 → 1 条 A×5，详情页一次设数量后加购，大幅降低操作次数与失败率。
        """
        merged: List[Dict[str, Any]] = []
        index_by_key: Dict[str, int] = {}
        for product in products:
            key = self._product_cart_key(str(product.get("url") or ""))
            if not key:
                # 无法识别时保留原样，避免误合并。
                merged.append(
                    {
                        **product,
                        "quantity": max(1, int(product.get("quantity") or 1)),
                        "_source_lines": [product],
                    }
                )
                continue
            try:
                qty = max(1, int(product.get("quantity") or 1))
            except Exception:
                qty = 1
            if key in index_by_key:
                item = merged[index_by_key[key]]
                item["quantity"] = int(item.get("quantity") or 0) + qty
                item["_source_lines"].append(product)
                continue
            index_by_key[key] = len(merged)
            row = dict(product)
            row["quantity"] = qty
            row["_source_lines"] = [product]
            merged.append(row)
        if len(merged) < len(products):
            self.logger.info(
                "乐天市场：接口商品行 %s 条已合并为 %s 次加购（按 URL+variantId）",
                len(products),
                len(merged),
            )
            for item in merged:
                self.logger.info(
                    "乐天市场：合并项 qty=%s lines=%s url=%s",
                    item.get("quantity"),
                    len(item.get("_source_lines") or []),
                    self._direct_product_url(str(item.get("url") or "")),
                )
        return merged

    def _expected_cart_quantities(
        self, expected_products: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """
        按真实商品 URL 路径聚合接口行。
        例如 A×1、A×1、A×1 聚合为 {A: 3}。
        """
        expected: Dict[str, int] = {}
        for product in expected_products:
            key = self._product_cart_key(str(product.get("url") or ""))
            if not key:
                continue
            try:
                quantity = int(product.get("quantity") or 1)
            except Exception:
                quantity = 1
            quantity = max(1, quantity)
            expected[key] = expected.get(key, 0) + quantity
        return expected

    def _read_initial_state(self, driver) -> Optional[Dict[str, Any]]:
        try:
            state = driver.execute_script("return window.__INITIAL_STATE__ || null;")
        except Exception:
            return None
        return state if isinstance(state, dict) else None

    def _parse_cart_totals(self, driver) -> Tuple[int, int, int]:
        """
        从购物车页读取金额（优先 window.__INITIAL_STATE__.shopItemSubtotals）。
        返回 (goods_fee, operate_fee, total)。

        确认页含电话号，易把 080-7535-8884 误解析成运费；购物车侧栏更干净。
        """
        self._ensure_cart_page(driver, quick=True)
        info = None
        try:
            info = driver.execute_script(
                """
                try {
                  var st = window.__INITIAL_STATE__ || {};
                  var subs = st.shopItemSubtotals || {};
                  var out = {
                    payment: 0, shipping: 0, itemTotal: 0, lineSum: 0,
                    shippingKnown: false, itemKnown: false, paymentKnown: false,
                    shopCount: 0, sampleKeys: []
                  };
                  function num(v) {
                    if (v === true || v === false || v === null || v === undefined) return null;
                    if (typeof v === 'string' && !v.trim()) return null;
                    var n = Number(v);
                    return isFinite(n) ? n : null;
                  }
                  function pick(obj, names) {
                    for (var i = 0; i < names.length; i++) {
                      var n = num(obj[names[i]]);
                      if (n !== null) return n;
                    }
                    return null;
                  }
                  Object.keys(subs).forEach(function(k) {
                    var s = subs[k] || {};
                    out.shopCount += 1;
                    if (out.sampleKeys.length < 12) {
                      try { out.sampleKeys = out.sampleKeys.concat(Object.keys(s).slice(0, 12)); } catch (e) {}
                    }
                    var pay = pick(s, [
                      'paymentAmount', 'totalPaymentAmount', 'payment',
                      'totalAmount', 'total', 'orderAmount'
                    ]);
                    var ship = pick(s, [
                      'shippingFee', 'shippingFeeTaxIncluded', 'postage',
                      'shippingCost', 'deliveryFee', 'shipping'
                    ]);
                    if (s.isShippingFree === true || s.shippingFree === true || s.freeShipping === true) {
                      ship = 0;
                    }
                    var it = pick(s, [
                      'itemTotal', 'itemsTotal', 'goodsTotal', 'merchandiseTotal',
                      'subTotal', 'subtotal', 'itemsPrice', 'itemPriceTotal'
                    ]);
                    if (pay !== null) { out.payment += pay; out.paymentKnown = true; }
                    if (ship !== null) { out.shipping += ship; out.shippingKnown = true; }
                    if (it !== null) { out.itemTotal += it; out.itemKnown = true; }
                  });
                  var shopItems = st.shopItems || {};
                  Object.keys(shopItems).forEach(function(sk) {
                    var shop = shopItems[sk] || {};
                    var container = shop.items;
                    var items = {};
                    if (container && container.items && typeof container.items === 'object') {
                      items = container.items;
                    } else if (container && typeof container === 'object') {
                      items = container;
                    }
                    Object.keys(items).forEach(function(ik) {
                      var it = items[ik] || {};
                      var price = Number(it.price) || 0;
                      var qty = Number(it.quantity) || 1;
                      if (qty < 1) qty = 1;
                      out.lineSum += price * qty;
                    });
                  });
                  return out;
                } catch (e) {
                  return { error: String(e) };
                }
                """
            )
        except Exception as e:
            self.logger.warning("乐天市场：读取购物车 INITIAL_STATE 金额失败: %s", e)
            info = None

        goods_fee = operate_fee = total = 0
        if isinstance(info, dict) and not info.get("error"):
            line_sum = int(info.get("lineSum") or 0)
            item_total = int(info.get("itemTotal") or 0) if info.get("itemKnown") else 0
            shipping = int(info.get("shipping") or 0) if info.get("shippingKnown") else None
            payment = int(info.get("payment") or 0) if info.get("paymentKnown") else 0

            goods_fee = item_total or line_sum
            if payment > 0:
                total = payment
            elif goods_fee > 0 and shipping is not None:
                total = goods_fee + int(shipping)
            elif goods_fee > 0:
                total = goods_fee

            if total > 0 and goods_fee > 0:
                operate_fee = total - goods_fee
            elif shipping is not None:
                operate_fee = int(shipping)

            self.logger.info(
                "乐天市场：购物车金额 state goods=%s operate=%s total=%s "
                "(itemTotal=%s lineSum=%s shipping=%s payment=%s keys=%s)",
                goods_fee,
                operate_fee,
                total,
                item_total,
                line_sum,
                shipping,
                payment,
                list(dict.fromkeys(info.get("sampleKeys") or []))[:20],
            )

        # DOM 兜底：购物车摘要区（仍做电话噪声过滤）
        if total <= 0 or goods_fee <= 0:
            try:
                src = driver.page_source or ""
            except Exception:
                src = ""
            # 送料無料
            shipping_dom = None
            if "送料無料" in src:
                shipping_dom = 0
            # 尝试侧栏数字
            try:
                # 常见「支払い金額」「合計」展示
                for sel in (
                    "[class*='number-display']",
                    "[class*='payment']",
                ):
                    els = self._find_elements_now(driver, By.CSS_SELECTOR, sel)
                    vals = []
                    for el in els[:30]:
                        try:
                            if not el.is_displayed():
                                continue
                            v = _pick_row_yen_amount(el.text or "")
                            if v > 0:
                                vals.append(v)
                        except Exception:
                            continue
                    if vals:
                        # 取最大的作为应付总额候选
                        cand_total = max(vals)
                        if total <= 0:
                            total = cand_total
                        break
            except Exception:
                pass
            if goods_fee <= 0 and total > 0 and shipping_dom == 0:
                goods_fee = total
                operate_fee = 0
            elif goods_fee <= 0 and total > 0 and shipping_dom is not None:
                goods_fee = max(0, total - int(shipping_dom))
                operate_fee = int(shipping_dom)

        if total > 0 and goods_fee > 0 and goods_fee + operate_fee != total:
            operate_fee = total - goods_fee

        if total <= 0 or goods_fee <= 0:
            raise RuntimeError(
                "购物车页未能解析总金额/商品金额（INITIAL_STATE/DOM），"
                "请确认已在 cart.step 且 shopItemSubtotals 可用"
            )
        return goods_fee, operate_fee, total

    def _cart_unit_prices_by_key(self, driver) -> Dict[str, int]:
        """购物车各商品 key → 单价（来自 INITIAL_STATE.shopItems）。"""
        state = self._read_initial_state(driver)
        out: Dict[str, int] = {}
        if not isinstance(state, dict):
            return out
        shop_items = state.get("shopItems")
        if not isinstance(shop_items, dict):
            return out
        for shop_data in shop_items.values():
            if not isinstance(shop_data, dict):
                continue
            items_container = shop_data.get("items")
            if isinstance(items_container, dict) and isinstance(
                items_container.get("items"), dict
            ):
                items = items_container.get("items") or {}
            elif isinstance(items_container, dict):
                items = items_container
            else:
                continue
            for item in items.values():
                if not isinstance(item, dict):
                    continue
                key = self._product_cart_key(str(item.get("itemUrl") or ""))
                if not key:
                    continue
                try:
                    price = int(round(float(item.get("price") or 0)))
                except Exception:
                    price = 0
                if price > 0:
                    out[key] = price
        return out

    def _build_check_cart_from_cart(
        self, cart_products: List[Dict[str, Any]], driver
    ) -> Tuple[List[Dict[str, Any]], int, int, int]:
        """在购物车页组装 checkCart 所需 GoodsList + 金额。"""
        store = self._store_name()
        price_by_key = self._cart_unit_prices_by_key(driver)
        goods_list: List[Dict[str, Any]] = []
        grouped: List[Dict[str, Any]] = []
        group_by_key: Dict[str, Dict[str, Any]] = {}
        for p in cart_products:
            no = self._line_no_for_check_cart(p)
            if not no:
                continue
            key = self._product_cart_key(str(p.get("url") or "")) or ("no:" + no)
            try:
                expected_num = max(1, int(p.get("quantity") or 1))
            except Exception:
                expected_num = 1
            if key in group_by_key:
                group_by_key[key]["quantity"] += expected_num
                continue
            group = {
                "key": key,
                "no": no,
                "quantity": expected_num,
                "product": p,
            }
            group_by_key[key] = group
            grouped.append(group)

        for group in grouped:
            p = group["product"]
            no = str(group["no"])
            num = int(group["quantity"])
            key = str(group["key"])
            price = int(price_by_key.get(key) or 0)
            if price <= 0:
                # 宽松匹配：按商品 path 不含 variant
                base = key.split("?")[0]
                for ck, pv in price_by_key.items():
                    if ck.split("?")[0] == base and pv > 0:
                        price = int(pv)
                        break
            if price <= 0:
                try:
                    price = int(round(float(p.get("price") or 0)))
                except Exception:
                    price = 0
            if num <= 0:
                num = 1
            goods_list.append({"No": no, "Num": num, "StoreName": store, "Price": price})

        goods_fee, operate_fee, total = self._parse_cart_totals(driver)
        if not goods_list:
            return [], total, goods_fee, operate_fee
        computed = sum(int(x["Price"]) * int(x["Num"]) for x in goods_list)
        if goods_fee <= 0 and computed > 0:
            goods_fee = computed
            if total > 0:
                operate_fee = total - goods_fee
        if total <= 0 and computed > 0:
            total = computed + operate_fee
        if total > 0 and goods_fee > 0 and goods_fee + operate_fee != total:
            operate_fee = total - goods_fee
        self.logger.info(
            "乐天市场：购物车组装 checkCart goods=%s GoodsFee=%s OperateFee=%s Total=%s",
            len(goods_list),
            goods_fee,
            operate_fee,
            total,
        )
        return goods_list, total, goods_fee, operate_fee

    def _actual_cart_quantities_from_state(self, driver) -> Optional[Dict[str, int]]:
        """
        从乐天购物车 window.__INITIAL_STATE__.shopItems 读取真实商品 URL、规格和数量。
        不依赖 React 编译 class。
        """
        try:
            state = driver.execute_script("return window.__INITIAL_STATE__ || null;")
        except Exception:
            return None
        if not isinstance(state, dict):
            return None
        shop_items = state.get("shopItems")
        if not isinstance(shop_items, dict):
            return None

        actual: Dict[str, int] = {}
        for shop_data in shop_items.values():
            if not isinstance(shop_data, dict):
                continue
            items_container = shop_data.get("items")
            if isinstance(items_container, dict) and isinstance(
                items_container.get("items"), dict
            ):
                items = items_container.get("items") or {}
            elif isinstance(items_container, dict):
                items = items_container
            else:
                continue
            for item in items.values():
                if not isinstance(item, dict):
                    continue
                key = self._product_cart_key(str(item.get("itemUrl") or ""))
                if not key:
                    continue
                try:
                    quantity = max(1, int(item.get("quantity") or 1))
                except Exception:
                    quantity = 1
                actual[key] = actual.get(key, 0) + quantity
        return actual

    def _actual_cart_quantities(self, driver) -> Optional[Dict[str, int]]:
        """优先读 __INITIAL_STATE__；若状态缺失则刷新一次再读，避免 eager 加载导致空状态误判。"""
        actual = self._actual_cart_quantities_from_state(driver)
        if actual is not None:
            return actual
        try:
            self._refresh_allow_timeout(driver)
            time.sleep(1.0)
            self._dismiss_interruptions(driver, timeout=1.0)
        except Exception:
            pass
        return self._actual_cart_quantities_from_state(driver)

    def _cart_contains_expected_products(
        self,
        driver,
        expected_products: List[Dict[str, Any]],
        *,
        quick: bool = False,
    ) -> bool:
        """进入购物车，按商品 URL 聚合并精确核验每种商品的累计数量。"""
        self._ensure_cart_page(driver, quick=quick)
        dismiss_timeout = 0.8 if quick else 1.5
        self._dismiss_interruptions(driver, timeout=dismiss_timeout)
        expected_quantities = self._expected_cart_quantities(expected_products)
        if not expected_quantities:
            self.logger.warning("乐天市场：无法从订单商品链接生成购物车数量校验项")
            return False

        timeout_key = (
            "cart_verify_quick_seconds" if quick else "cart_verify_after_add_seconds"
        )
        default_timeout = 12.0 if quick else 45.0
        poll_interval = 0.35 if quick else 0.8
        deadline = time.time() + float(self.ri_cfg.get(timeout_key, default_timeout))
        last_actual: Optional[Dict[str, int]] = None
        while time.time() < deadline:
            actual_quantities = self._actual_cart_quantities(driver)
            last_actual = actual_quantities
            if actual_quantities is None:
                self.logger.warning(
                    "乐天市场：购物车状态未就绪，重新打开购物车后重试核验"
                )
                try:
                    self._navigate(driver, self._cart_url())
                    retry_wait = float(
                        self.ri_cfg.get("wait_after_cart_load_seconds", 2)
                    )
                    time.sleep(min(retry_wait, 1.0) if quick else retry_wait + 1.0)
                    self._dismiss_interruptions(driver, timeout=dismiss_timeout)
                except Exception as e:
                    self.logger.warning("乐天市场：重新打开购物车失败: %s", e)
                time.sleep(0.5 if quick else 1.0)
                continue
            if actual_quantities == expected_quantities:
                self.logger.info(
                    "乐天市场：购物车商品及数量核验通过%s，期望=%s，实际=%s",
                    "（快速）" if quick else "",
                    expected_quantities,
                    actual_quantities,
                )
                return True
            if self._cart_quantity_maps_equal(expected_quantities, actual_quantities):
                self.logger.info(
                    "乐天市场：购物车商品及数量核验通过（按商品ID匹配%s），期望=%s，实际=%s",
                    "，快速" if quick else "",
                    expected_quantities,
                    actual_quantities,
                )
                return True

            time.sleep(poll_interval)
        self.logger.warning(
            "乐天市场：购物车商品或累计数量不一致，期望=%s，实际=%s",
            expected_quantities,
            last_actual,
        )
        return False

    def _prepare_product_page(
        self,
        driver,
        product_url: str,
        quantity: int,
        *,
        claim_coupon: bool,
        try_set_quantity: bool = True,
    ) -> str:
        direct_url = self._direct_product_url(product_url)
        if direct_url != product_url:
            self.logger.info("乐天市场：已将跟踪链接转换为商品直链: %s", direct_url)
        self._navigate(driver, direct_url)
        time.sleep(float(self.ri_cfg.get("wait_after_pdp_load_seconds", 2)))
        self._dismiss_interruptions(driver)
        # 部分店铺（如楽天24エクスプレス）会跳转到子域，记录最终 URL 便于排查
        try:
            final_url = (driver.current_url or "").strip()
            if final_url and final_url.split("#")[0] != direct_url.split("#")[0]:
                self.logger.info("乐天市场：商品页发生跳转 %s → %s", direct_url, final_url)
                direct_url = final_url.split("#")[0]
        except Exception:
            final_url = ""
        # 跳到书店站后：抛出转交信号（整单未加购时由 process_order 转交书店流程）
        if is_rakuten_books_product_url(direct_url) or is_rakuten_books_product_url(
            final_url or ""
        ):
            raise RakutenBooksHandoffNeeded(direct_url or final_url)
        selected_variant = self._select_product_requirements(driver, direct_url)
        if selected_variant:
            stamped = self._stamp_variant_id_on_url(direct_url, selected_variant)
            if stamped != direct_url:
                direct_url = stamped
        if try_set_quantity:
            self._try_set_quantity_on_pdp(driver, quantity)
        # 领券已关闭：打开券页极慢且本店几乎领不到，会显著拉高多件失败率。
        if claim_coupon and self.ri_cfg.get("coupon_claim_enabled", False):
            self._try_claim_coupon(driver)
        return direct_url

    def _click_add_and_confirm(self, driver, prefer_fixed: bool, target_qty: int) -> None:
        # 加购前再扫一次：部分店铺确认框在选规格后才渲染
        try:
            clicked = self._accept_required_option_checkboxes(driver)
            # 药店等：勾选「理解/了承」后按钮才可点，稍等渲染
            if clicked:
                time.sleep(0.45)
        except Exception as e:
            self.logger.debug("乐天市场：加购前勾选确认项异常（忽略）: %s", e)
        self._click_add_to_cart_once(driver, prefer_fixed=prefer_fixed)
        time.sleep(float(self.ri_cfg.get("wait_after_add_click_seconds", 1.2)))
        self._try_set_quantity_in_dialog(driver, target_qty)
        self._confirm_sku_modal_if_needed(driver)

    def _reconcile_product_quantity(
        self,
        driver,
        product_url: str,
        product_key: str,
        target_qty: int,
        expected_products: List[Dict[str, Any]],
    ) -> bool:
        """
        数量兜底：详情页设数量 → 加购 → 购物车改数量 → 逐件补加。
        任一环节成功且购物车总量匹配即通过。
        """
        if self._cart_contains_expected_products(driver, expected_products, quick=True):
            return True

        current = self._get_cart_quantity_for_key(driver, product_key)
        if current > 0 and current != target_qty:
            # 不足上调、超量下调（速配 mart 等曾出现补加冲到 5）
            if self._try_set_quantity_in_cart(driver, product_key, target_qty):
                if self._cart_contains_expected_products(
                    driver, expected_products, quick=True
                ):
                    return True

        max_attempts = max(2, max(0, target_qty - max(current, 0)) + 3)
        for attempt in range(max_attempts):
            current = self._get_cart_quantity_for_key(driver, product_key)
            if self._cart_contains_expected_products(
                driver, expected_products, quick=True
            ):
                return True
            if current > target_qty:
                self.logger.warning(
                    "乐天市场：购物车超量，尝试下调 key=%s 当前=%s 目标=%s",
                    product_key,
                    current,
                    target_qty,
                )
                if self._try_set_quantity_in_cart(driver, product_key, target_qty):
                    if self._cart_contains_expected_products(
                        driver, expected_products, quick=True
                    ):
                        return True
                break
            if current >= target_qty:
                break
            need = target_qty - current
            self.logger.info(
                "乐天市场：逐件补加 key=%s 当前=%s 目标=%s 仍需=%s（%s/%s）",
                product_key,
                current,
                target_qty,
                need,
                attempt + 1,
                max_attempts,
            )
            # 必须强制把详情页数量设为 1，避免上次残留数量导致一次加多件
            self._prepare_product_page(
                driver,
                product_url,
                1,
                claim_coupon=False,
                try_set_quantity=True,
            )
            clicked = False
            for prefer_fixed in (True, False):
                try:
                    self._click_add_and_confirm(driver, prefer_fixed, 1)
                    clicked = True
                    break
                except Exception as e:
                    self.logger.warning(
                        "乐天市场：逐件补加点击失败 prefer_fixed=%s: %s",
                        prefer_fixed,
                        e,
                    )
            if not clicked:
                break
            time.sleep(float(self.ri_cfg.get("wait_after_add_click_seconds", 1.2)))

        # 补加循环结束后若仍超量，再尝试一次下调
        current = self._get_cart_quantity_for_key(driver, product_key)
        if current > target_qty:
            self.logger.warning(
                "乐天市场：补加后仍超量，最后尝试下调 key=%s 当前=%s 目标=%s",
                product_key,
                current,
                target_qty,
            )
            self._try_set_quantity_in_cart(driver, product_key, target_qty)

        return self._cart_contains_expected_products(
            driver, expected_products, quick=False
        )

    @staticmethod
    def _item_page_data(driver) -> Dict[str, Any]:
        try:
            script = driver.find_element(By.CSS_SELECTOR, "script#item-page-app-data")
            raw = script.get_attribute("textContent") or script.get_attribute("innerHTML") or ""
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _stamp_variant_id_on_url(self, url: str, variant_id: str) -> str:
        """
        详情页已选定规格时，把 variantId 写回链接，便于购物车键与实际一致。
        规则：URL 里已有 variantId 则原样返回（绝不覆盖/删除）；仅在缺失时追加。
        """
        vid = str(variant_id or "").strip()
        if not vid or not url:
            return url
        try:
            parsed = urlparse(url)
            q = parse_qs(parsed.query, keep_blank_values=True)
            existing = (q.get("variantId") or q.get("variantid") or [""])[0].strip()
            if existing:
                # 已有规格 ID：原样保留，不做任何改写
                return url
            # 保留其它查询参数，仅追加 variantId
            from urllib.parse import urlencode

            flat: List[Tuple[str, str]] = []
            for k, vals in q.items():
                if k.lower() == "variantid":
                    continue
                for v in vals:
                    flat.append((k, v))
            flat.append(("variantId", vid))
            return urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    urlencode(flat, doseq=True),
                    parsed.fragment,
                )
            )
        except Exception:
            sep = "&" if "?" in url else "?"
            return "%s%svariantId=%s" % (url, sep, vid)

    def _select_product_requirements(self, driver, direct_url: str) -> str:
        """根据 variantId 选择 SKU，并填写卖家要求的全部确认下拉框。返回最终选用的 variantId（可空）。"""
        data = self._item_page_data(driver)
        api = data.get("api") if isinstance(data, dict) else {}
        api_data = api.get("data") if isinstance(api, dict) else {}
        info = api_data.get("itemInfoSku") if isinstance(api_data, dict) else {}
        if not isinstance(info, dict):
            info = {}

        purchase_info = info.get("purchaseInfo") or {}
        by_sell_type = (
            purchase_info.get("purchaseBySellType")
            if isinstance(purchase_info, dict)
            else {}
        ) or {}
        condition = str(by_sell_type.get("purchaseCondition") or "").strip().lower()
        if condition and condition != "enabled":
            raise RuntimeError("商品当前不可购买，purchaseCondition=%s" % condition)

        parsed = urlparse(direct_url)
        query = parse_qs(parsed.query)
        variant_id = (
            (query.get("variantId") or [""])[0].strip()
            or (query.get("variantid") or [""])[0].strip()
        )
        # 页面数据在不同模板中会把 SKU 放在 itemInfoSku.sku 或 purchaseInfo.sku；
        # 递归收集，避免只读其中一份导致合法 variantId 被误判为不存在。
        sku_list: List[Dict[str, Any]] = []
        seen_variants = set()
        stack: List[Any] = [info]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                node_variant = str(node.get("variantId") or "").strip()
                if (
                    node_variant
                    and isinstance(node.get("selectorValues"), list)
                    and node_variant not in seen_variants
                ):
                    seen_variants.add(node_variant)
                    sku_list.append(node)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        target_sku: Optional[Dict[str, Any]] = None
        if variant_id:
            for sku in sku_list:
                if str((sku or {}).get("variantId") or "") == variant_id:
                    target_sku = sku
                    break
            if sku_list and target_sku is None:
                raise RuntimeError("商品 URL 的 variantId 未在页面 SKU 数据中找到: %s" % variant_id)
        elif len(sku_list) == 1:
            target_sku = sku_list[0]
        elif len(sku_list) > 1:
            raise RuntimeError("多规格商品 URL 缺少 variantId，不能自动选择规格")

        selected_variant = ""
        if target_sku:
            selected_variant = str(
                variant_id or target_sku.get("variantId") or ""
            ).strip()
            selector_values = target_sku.get("selectorValues") or []
            for value in selector_values:
                value = str(value or "").strip()
                if not value:
                    continue
                clicked = False
                deadline = time.time() + float(
                    self.ri_cfg.get("sku_selector_wait_seconds", 15)
                )
                while time.time() < deadline and not clicked:
                    for button in self._find_elements_now(
                        driver,
                        By.CSS_SELECTOR,
                        'tr[irc="SkuSelectionArea"] button[aria-label]',
                    ):
                        try:
                            if (
                                button.is_displayed()
                                and button.is_enabled()
                                and (button.get_attribute("aria-label") or "").strip() == value
                            ):
                                driver.execute_script("arguments[0].click();", button)
                                clicked = True
                                time.sleep(0.35)
                                break
                        except Exception:
                            continue
                    if not clicked:
                        time.sleep(0.3)
                if not clicked:
                    raise RuntimeError("未找到 SKU 规格按钮: %s" % value)
            self.logger.info(
                "乐天市场：已选择 variantId=%s，规格=%s",
                selected_variant,
                selector_values,
            )

        # customizationOptions 中即使 required=false，页面也可能要求确认；全部选择有效项。
        option_selects = []
        has_customization_data = bool(info.get("customizationOptions"))
        option_deadline = time.time() + (
            float(self.ri_cfg.get("seller_option_wait_seconds", 5))
            if has_customization_data
            else 0.01
        )
        while time.time() < option_deadline:
            option_selects = self._find_elements_now(
                driver, By.CSS_SELECTOR, 'tr[irc="OptionArea"] select'
            )
            if option_selects:
                break
            time.sleep(0.25)
        for select_el in option_selects:
            try:
                select = Select(select_el)
                valid_options = []
                for option in select.options:
                    text = (option.text or "").strip()
                    value = (option.get_attribute("value") or "").strip()
                    if text and text != "選択してください" and value:
                        valid_options.append((value, text))
                if not valid_options:
                    continue
                selected_text = (select.first_selected_option.text or "").strip()
                if selected_text == "選択してください" or not selected_text:
                    select.select_by_value(valid_options[0][0])
                    time.sleep(0.25)
            except Exception as e:
                raise RuntimeError("填写卖家确认选项失败: %s" % e) from e
        if option_selects:
            self.logger.info("乐天市场：已处理卖家确认选项 %s 项", len(option_selects))

        # 卖家确认复选框（如 Amazon マルチチャネル「確認したので、了承の上購入する。」）
        # 未勾选时点击「かごに追加」往往无效果，购物车也核验不到商品。
        self._accept_required_option_checkboxes(driver)

        # React 状态更新有短暂延迟，等待可见的「未選択」警告消失。
        warning_deadline = time.time() + float(
            self.ri_cfg.get("required_selection_verify_seconds", 5)
        )
        while True:
            warning_visible = False
            for el in self._find_elements_now(
                driver,
                By.XPATH,
                "//*[contains(normalize-space(text()), '未選択の項目があります')]",
            ):
                try:
                    warning_visible = warning_visible or el.is_displayed()
                except Exception:
                    continue
            if not warning_visible:
                break
            if time.time() >= warning_deadline:
                raise RuntimeError("商品仍有未选择的必填规格或卖家确认项")
            time.sleep(0.25)
        return selected_variant

    def _accept_required_option_checkboxes(self, driver) -> int:
        """
        勾选 OptionArea / 页面上的必填确认复选框。
        典型文案：「確認したので、了承の上購入する。」（必須）
        """
        wait_sec = float(self.ri_cfg.get("seller_option_checkbox_wait_seconds", 4))
        deadline = time.time() + max(0.5, wait_sec)
        boxes = []
        while time.time() < deadline:
            boxes = self._find_elements_now(
                driver,
                By.CSS_SELECTOR,
                'tr[irc="OptionArea"] input[type="checkbox"], '
                "tr.normal-reserve-optionArea input[type=\"checkbox\"]",
            )
            if boxes:
                break
            # 部分模板未挂 irc，用了承文案兜底
            for xp in (
                "//label[.//*[contains(normalize-space(.),'了承')]]"
                "//input[@type='checkbox']",
                "//label[.//*[contains(normalize-space(.),'確認した')]]"
                "//input[@type='checkbox']",
                "//div[contains(@class,'control-group')]"
                "[.//*[contains(normalize-space(.),'了承')]]"
                "//input[@type='checkbox']",
            ):
                found = self._find_elements_now(driver, By.XPATH, xp)
                if found:
                    boxes = found
                    break
            if boxes:
                break
            time.sleep(0.25)

        skip_tokens = (
            "メルマガ",
            "メールマガジン",
            "ニュースレター",
            "お知らせメール",
        )
        confirm_tokens = (
            "了承",
            "確認した",
            "同意",
            "承諾",
            "購入する",
            "理解した",
        )
        clicked = 0
        seen = set()
        for box in boxes:
            try:
                uid = id(box)
                if uid in seen:
                    continue
                seen.add(uid)
                if not box.is_displayed():
                    continue
                try:
                    if box.is_selected():
                        continue
                except Exception:
                    pass

                context = ""
                try:
                    label = box.find_element(By.XPATH, "./ancestor::label[1]")
                    context = (label.text or "").strip()
                except Exception:
                    pass
                if not context:
                    try:
                        area = box.find_element(
                            By.XPATH,
                            "./ancestor::tr[@irc='OptionArea' or "
                            "contains(@class,'optionArea')][1]",
                        )
                        context = (area.text or "").strip()[:800]
                    except Exception:
                        context = ""

                if any(tok in context for tok in skip_tokens):
                    continue
                # OptionArea 内默认勾选；区外仅勾选确认类文案
                in_option_area = False
                try:
                    box.find_element(
                        By.XPATH,
                        "./ancestor::tr[@irc='OptionArea' or "
                        "contains(@class,'optionArea')][1]",
                    )
                    in_option_area = True
                except Exception:
                    in_option_area = False
                if not in_option_area and not any(
                    tok in context for tok in confirm_tokens
                ):
                    continue

                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
                    box,
                )
                time.sleep(0.12)
                try:
                    driver.execute_script("arguments[0].click();", box)
                except Exception:
                    box.click()
                time.sleep(0.15)
                try:
                    still_off = not box.is_selected()
                except Exception:
                    still_off = True
                if still_off:
                    try:
                        label = box.find_element(By.XPATH, "./ancestor::label[1]")
                        driver.execute_script("arguments[0].click();", label)
                    except Exception:
                        try:
                            # 点旁边文案节点
                            txt = box.find_element(
                                By.XPATH,
                                "./following::*[contains(@class,'text-display')][1]",
                            )
                            driver.execute_script("arguments[0].click();", txt)
                        except Exception:
                            pass
                clicked += 1
                self.logger.info(
                    "乐天市场：已勾选确认复选框（%s）",
                    (context[:60].replace("\n", " ") if context else "OptionArea"),
                )
                time.sleep(0.2)
            except Exception as e:
                self.logger.debug("乐天市场：勾选确认复选框跳过: %s", e)
                continue

        if clicked:
            self.logger.info("乐天市场：共勾选卖家确认复选框 %s 项", clicked)
        return clicked

    def _add_product_to_cart(
        self,
        driver,
        product: Dict[str, Any],
        expected_products: List[Dict[str, Any]],
    ) -> None:
        """加购商品。无「かごに追加」时点击「購入手続きへ」（等价加购）。"""
        product_url = str(product.get("url") or "").strip()
        target_qty = max(1, int(product.get("quantity") or 1))
        product_key = self._product_cart_key(product_url)

        # 1) 详情页尝试设数量后加购（固定 → 浮动）
        # 任一路径只要点过加购，即使数量不够也走 reconcile（购物车改数 / 逐件补加）
        any_clicked = False
        for prefer_fixed in (True, False):
            label = "固定区域" if prefer_fixed else "浮动区域"
            try:
                prepared_url = self._prepare_product_page(
                    driver,
                    product_url,
                    target_qty,
                    claim_coupon=prefer_fixed,
                    try_set_quantity=True,
                )
                # 详情页选定规格后，把带 variantId 的键写回商品，避免期望键无规格、购物车有规格
                stamped_key = self._product_cart_key(prepared_url)
                if stamped_key and stamped_key != product_key:
                    product["url"] = prepared_url
                    product_url = prepared_url
                    product_key = stamped_key
                self.logger.info(
                    "乐天市场：尝试主加购方式：%s（かごに追加 / 購入手続きへ）",
                    label,
                )
                self._click_add_and_confirm(driver, prefer_fixed, target_qty)
                any_clicked = True
                if self._reconcile_product_quantity(
                    driver, product_url, product_key, target_qty, expected_products
                ):
                    return
            except Exception as e:
                self.logger.warning("乐天市场：%s加购尝试失败: %s", label, e)
                # 书店跳转无法用市场按钮加购，不必再试浮动区域
                if "乐天书店" in str(e):
                    raise

        # 2) 已加进车但数量不够时，最后再兜底一次（避免浮动按钮找不到就整单失败）
        if any_clicked and self._reconcile_product_quantity(
            driver, product_url, product_key, target_qty, expected_products
        ):
            return

        raise RuntimeError("固定区域及浮动区域加购后，购物车均未核验到预期商品")

    def _try_claim_coupon(self, driver) -> None:
        """领券默认关闭。历史逻辑保留但不执行，避免打开券页拖慢整单。"""
        if self.ri_cfg.get("coupon_claim_enabled", False) is not True:
            return
        # 以下仅在显式开启 coupon_claim_enabled=true 时执行。
        coupon_links = self._find_elements_now(
            driver,
            By.CSS_SELECTOR,
            '.sale_desc a[href*="coupon"], a[href*="coupon.rakuten.co.jp"]',
        )
        for link in coupon_links:
            original_handle = None
            try:
                href = (link.get_attribute("href") or "").strip()
                if not href:
                    continue
                self._random_pre_click_wait("领取图片优惠券")
                original_handle = driver.current_window_handle
                original_handles = set(driver.window_handles)
                driver.execute_script("window.open(arguments[0], '_blank');", href)
                WebDriverWait(driver, 8).until(
                    lambda d: len(set(d.window_handles) - original_handles) > 0
                )
                new_handle = next(iter(set(driver.window_handles) - original_handles))
                driver.switch_to.window(new_handle)
                time.sleep(float(self.ri_cfg.get("wait_after_coupon_click_seconds", 1.5)))
                page_text = driver.find_element(By.TAG_NAME, "body").text
                success_texts = (
                    "クーポンを獲得しました",
                    "獲得済み",
                    "クーポン獲得済み",
                    "このクーポンは獲得済みです",
                )
                verified = any(
                    text in page_text
                    for text in success_texts
                )
                if not verified:
                    claim_targets = self._find_elements_now(
                        driver,
                        By.XPATH,
                        "//button[contains(., 'クーポンを獲得') or contains(., '獲得する')]"
                        "|//a[contains(., 'クーポンを獲得') or contains(., '獲得する')]",
                    )
                    for target in claim_targets:
                        if target.is_displayed() and target.is_enabled():
                            driver.execute_script("arguments[0].click();", target)
                            time.sleep(
                                float(
                                    self.ri_cfg.get(
                                        "wait_after_coupon_click_seconds", 1.5
                                    )
                                )
                            )
                            page_text = driver.find_element(By.TAG_NAME, "body").text
                            verified = any(text in page_text for text in success_texts)
                            break
                driver.close()
                driver.switch_to.window(original_handle)
                # 领券可能把详情页弄到后台；回到原页再继续加购。
                try:
                    if "item.rakuten.co.jp" not in (driver.current_url or "").lower():
                        driver.switch_to.window(original_handle)
                except Exception:
                    pass
                if verified:
                    self.logger.info("乐天市场：图片优惠券领取状态已确认")
                else:
                    self.logger.warning(
                        "乐天市场：已打开图片优惠券页面，但未识别到领取成功文本"
                    )
                return
            except Exception as e:
                try:
                    if len(driver.window_handles) > 1:
                        driver.close()
                    if original_handle:
                        driver.switch_to.window(original_handle)
                except Exception:
                    pass
                self.logger.warning("乐天市场：图片优惠券领取失败: %s", e)

        xpaths = [
            "//button[contains(@aria-label, 'クーポン')]",
            "//button[contains(., 'クーポンを獲得')]",
            "//button[contains(., '獲得する')]",
            "//a[contains(., 'クーポン') and contains(., '獲得')]",
        ]
        for xp in xpaths:
            try:
                for el in self._find_elements_now(driver, By.XPATH, xp):
                    if not el.is_displayed():
                        continue
                    self._random_pre_click_wait("领取优惠券")
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(float(self.ri_cfg.get("wait_after_coupon_click_seconds", 1.5)))
                    self._dismiss_interruptions(driver, timeout=2.0)
                    self.logger.info("乐天市场：已尝试领取优惠券")
                    return
            except Exception:
                continue
        self.logger.warning("乐天市场：未找到可点击的优惠券领取按钮（已跳过，继续流程）")

    def _parse_confirm_page_lines(self, driver) -> List[Tuple[int, int]]:
        """确认页：按 #items-in-order-section 内商品块顺序解析 (单价, 数量)。"""
        rows: List[Tuple[int, int]] = []
        section_sel = (self.ri_cfg.get("confirm_items_section_css") or "#items-in-order-section").strip()
        try:
            section = driver.find_element(By.CSS_SELECTOR, section_sel)
            blocks = section.find_elements(
                By.XPATH,
                ".//div[contains(@class,'background-layer')]/ancestor::div[contains(@class,'block--')][1]",
            )
        except Exception:
            blocks = []
        if not blocks:
            try:
                blocks = driver.find_elements(
                    By.CSS_SELECTOR,
                    "%s .number-display--3UwWM" % section_sel,
                )
            except Exception:
                blocks = []

        if blocks and blocks[0].tag_name.lower() == "div" and "number-display" in (
            blocks[0].get_attribute("class") or ""
        ):
            for nd in blocks:
                try:
                    price = _pick_row_yen_amount(nd.text or "") or _parse_yen_int(nd.text or "")
                    rows.append((price, 1))
                except Exception:
                    rows.append((0, 1))
            return rows

        for block in blocks:
            price = 0
            num = 1
            try:
                nd = block.find_element(By.CSS_SELECTOR, ".number-display--3UwWM")
                price = _pick_row_yen_amount(nd.text or "") or _parse_yen_int(nd.text or "")
            except Exception:
                pass
            try:
                sel = block.find_element(By.CSS_SELECTOR, "select.select--3Nrso")
                opt = sel.find_element(By.CSS_SELECTOR, "option[selected]")
                num = int(opt.get_attribute("value") or opt.text or "1")
            except Exception:
                try:
                    num = int(
                        re.sub(
                            r"[^\d]",
                            "",
                            block.find_element(By.CSS_SELECTOR, "select.select--3Nrso").get_attribute("value")
                            or "1",
                        )
                        or "1"
                    )
                except Exception:
                    num = 1
            if num <= 0:
                num = 1
            rows.append((price, num))
        return rows

    def _parse_confirm_totals(self, driver) -> Tuple[int, int, int]:
        """
        返回 (goods_fee, operate_fee, total)。

        页面常见：小計 / 送料 / クーポン・ポイント / 支払い金額。
        后端 checkCart 要求 GoodsFee + OperateFee == Total。
        OperateFee 最终一律用 Total - GoodsFee，避免把电话号/积分文案误当成运费或优惠。
        """
        goods_fee = shipping = total = 0
        coupon = 0

        def _row_amount(label: str, *, exact: bool = True) -> Tuple[int, bool]:
            try:
                if exact:
                    xp = (
                        "//span[normalize-space()='%s']"
                        "/ancestor::div[contains(@class,'flex-row')][1]"
                        % label
                    )
                else:
                    xp = (
                        "//span[contains(normalize-space(),'%s')]"
                        "/ancestor::div[contains(@class,'flex-row')][1]"
                        % label
                    )
                row = driver.find_element(By.XPATH, xp)
                txt = row.text or ""
                if label == "送料" and "送料無料" in txt:
                    return 0, True
                # 只信任行内价格节点，避免整行混入电话/地址
                try:
                    for sel in (
                        ".number-display--3UwWM",
                        ".number-display--1oihd",
                        "[class*='number-display']",
                    ):
                        els = row.find_elements(By.CSS_SELECTOR, sel)
                        for el in els:
                            v = _pick_row_yen_amount(el.text or "")
                            if v > 0:
                                return v, True
                            # 送料免费常见显示 0
                            if label == "送料" and _parse_yen_int(el.text or "") == 0:
                                return 0, True
                except Exception:
                    pass
                # 整行兜底也先去电话噪声
                return _pick_row_yen_amount(txt), True
            except Exception:
                return 0, False

        v, ok = _row_amount("小計")
        if ok:
            goods_fee = v
        v, ok = _row_amount("送料")
        if ok:
            shipping = v

        # 优惠项：避免用过于宽泛的「ポイント」（会误伤获得积分/说明文案）
        for label, exact in (
            ("クーポン利用", True),
            ("クーポン", True),
            ("ポイント利用", True),
            ("値引き", True),
            ("割引", True),
        ):
            try:
                amt, found = _row_amount(label, exact=exact)
                if found and amt > 0:
                    coupon = amt
                    self.logger.info("乐天市场：确认页优惠项 %s = %s", label, amt)
                    break
            except Exception:
                continue

        try:
            el = driver.find_element(
                By.CSS_SELECTOR,
                ".number-display--1oihd .value--21p0x, div.number-display--1oihd div.value--21p0x",
            )
            total = _pick_row_yen_amount(el.text or "") or _parse_yen_int(el.text or "")
        except Exception:
            pass
        if total <= 0:
            v, ok = _row_amount("支払い金額")
            if ok:
                total = v

        if goods_fee <= 0 and total > 0:
            goods_fee = max(0, total - shipping + coupon)

        # 关键：运费差额 = 实付 - 商品小计（优惠自然体现为 0 或负数）
        # 不再用「送料 - 误解析的电话号」去减，避免 OperateFee=-8884 这类脏值
        if total > 0 and goods_fee > 0:
            operate_fee = total - goods_fee
        else:
            operate_fee = int(shipping) - int(coupon)

        # 合理性兜底：若仍异常且能拿到送料，用送料-优惠再对齐一次
        if total > 0 and goods_fee > 0 and abs(operate_fee) > max(total, goods_fee):
            self.logger.warning(
                "乐天市场：OperateFee 异常(%s)，回退 shipping-coupon (%s-%s)",
                operate_fee,
                shipping,
                coupon,
            )
            operate_fee = int(shipping) - int(coupon)
            if goods_fee + operate_fee != total:
                operate_fee = total - goods_fee

        self.logger.info(
            "乐天市场：确认页金额 goods=%s operate=%s total=%s (shipping=%s coupon=%s)",
            goods_fee,
            operate_fee,
            total,
            shipping,
            coupon,
        )
        return goods_fee, operate_fee, total

    def _build_check_cart_goods_list(
        self,
        cart_products: List[Dict[str, Any]],
        page_lines: List[Tuple[int, int]],
        driver,
    ) -> Tuple[List[Dict[str, Any]], int, int, int]:
        store = self._store_name()
        goods_list: List[Dict[str, Any]] = []
        grouped: List[Dict[str, Any]] = []
        group_by_key: Dict[str, Dict[str, Any]] = {}
        for p in cart_products:
            no = self._line_no_for_check_cart(p)
            if not no:
                continue
            key = self._product_cart_key(str(p.get("url") or "")) or ("no:" + no)
            try:
                expected_num = max(1, int(p.get("quantity") or 1))
            except Exception:
                expected_num = 1
            if key in group_by_key:
                group_by_key[key]["quantity"] += expected_num
                continue
            group = {
                "key": key,
                "no": no,
                "quantity": expected_num,
                "product": p,
            }
            group_by_key[key] = group
            grouped.append(group)

        if page_lines and len(page_lines) != len(grouped):
            raise RuntimeError(
                "结算页商品行数与按 URL+variantId 聚合后的接口商品数不一致: 页面=%s, 接口=%s"
                % (len(page_lines), len(grouped))
            )

        for i, group in enumerate(grouped):
            p = group["product"]
            no = str(group["no"])
            expected_num = int(group["quantity"])
            if i < len(page_lines):
                price, num = page_lines[i]
                if num != expected_num:
                    raise RuntimeError(
                        "结算页商品数量不一致: key=%s, 页面=%s, 接口聚合=%s"
                        % (group["key"], num, expected_num)
                    )
            else:
                try:
                    price = int(round(float(p.get("price") or 0)))
                except Exception:
                    price = 0
                num = expected_num
            if num <= 0:
                num = 1
            goods_list.append({"No": no, "Num": num, "StoreName": store, "Price": price})

        goods_fee, operate_fee, total = self._parse_confirm_totals(driver)
        if not goods_list:
            return [], total, goods_fee, operate_fee
        computed = sum(int(x["Price"]) * int(x["Num"]) for x in goods_list)
        if goods_fee <= 0 and computed > 0:
            goods_fee = computed
        if total <= 0 and computed > 0:
            total = computed + operate_fee
        return goods_list, total, goods_fee, operate_fee

    def _cart_purchase_block_reason(self, driver) -> str:
        """
        购物车数量可能与订单一致，但店铺限购仍会禁用「購入手続き」。
        典型文案：注文個数が購入可能数を超えております。
        INITIAL_STATE：shopItemSubtotals.*.enablePurchase === false
        """
        hints: List[str] = []
        try:
            src = driver.page_source or ""
        except Exception:
            src = ""
        limit_kw = (
            self.ri_cfg.get("cart_purchase_limit_text")
            or "注文個数が購入可能数を超えております"
        ).strip()
        if limit_kw and limit_kw in src:
            hints.append(limit_kw)

        # 页面警告条 / role=alert
        try:
            for el in self._find_elements_now(
                driver, By.CSS_SELECTOR, '[role="alert"], .container-warning--3r5zm'
            ):
                try:
                    if not el.is_displayed():
                        continue
                    t = (el.text or "").strip()
                except Exception:
                    continue
                if t and ("購入可能" in t or "注文個数" in t or "超えて" in t):
                    if t not in hints:
                        hints.append(t[:120])
        except Exception:
            pass

        # enablePurchase=false（任一店铺）
        try:
            blocked = driver.execute_script(
                """
                try {
                  var st = window.__INITIAL_STATE__ || {};
                  var subs = st.shopItemSubtotals || {};
                  var out = [];
                  Object.keys(subs).forEach(function(k) {
                    var s = subs[k] || {};
                    if (s.enablePurchase === false) out.push(String(k));
                  });
                  return out;
                } catch (e) { return []; }
                """
            )
            if blocked:
                hints.append("enablePurchase=false shops=%s" % ",".join(map(str, blocked)))
        except Exception:
            pass

        # 按钮存在但 disabled
        sel = (
            self.ri_cfg.get("shop_checkout_button_css")
            or 'button[aria-label="購入手続き"]'
        ).strip()
        seen_disabled = False
        try:
            for b in self._find_elements_now(driver, By.CSS_SELECTOR, sel):
                try:
                    if not b.is_displayed():
                        continue
                    disabled = bool(b.get_attribute("disabled"))
                    aria_dis = (b.get_attribute("aria-disabled") or "").lower()
                    cls = (b.get_attribute("class") or "").lower()
                    if (
                        disabled
                        or aria_dis in ("true", "1")
                        or "button-disabled" in cls
                        or "type-primary-disabled" in cls
                    ):
                        seen_disabled = True
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if seen_disabled:
            hints.append("「購入手続き」按钮已禁用")

        if not hints:
            return ""
        # 有限购文案或明确 enablePurchase=false 时视为限购阻断
        if any(
            ("購入可能" in h)
            or ("注文個数" in h)
            or ("超えて" in h)
            or h.startswith("enablePurchase=false")
            for h in hints
        ):
            return "购物车限购无法结算: " + "；".join(hints[:3])
        if seen_disabled:
            return "购物车「購入手続き」不可点击: " + "；".join(hints[:3])
        return ""

    def _shop_checkout(self, driver) -> None:
        self._ensure_cart_page(driver, quick=False)
        self._dismiss_interruptions(driver)
        # 改数后可能短暂禁用，先等一轮；若仍因限购禁用则明确失败
        self._wait_cart_update_settled(driver, timeout=8.0)
        block = self._cart_purchase_block_reason(driver)
        if block:
            raise RuntimeError(block)
        sel = (self.ri_cfg.get("shop_checkout_button_css") or 'button[aria-label="購入手続き"]').strip()
        btns = self._find_elements_now(driver, By.CSS_SELECTOR, sel)
        target = None
        for b in btns:
            try:
                if b.is_displayed() and b.is_enabled():
                    target = b
                    break
            except Exception:
                continue
        if target is None:
            # 再读一次限购原因，避免误报「未找到」
            block2 = self._cart_purchase_block_reason(driver)
            if block2:
                raise RuntimeError(block2)
            raise RuntimeError("购物车页未找到店铺「購入手続き」按钮")
        self._random_pre_click_wait("购物车購入手続き")
        driver.execute_script("arguments[0].click();", target)
        time.sleep(float(self.ri_cfg.get("wait_after_shop_checkout_seconds", 4)))
        # 偶发中间页：お届け先 → 点「次へ」进入注文確認
        self._pass_delivery_address_step_if_present(driver)

    def _is_delivery_address_step(self, driver) -> bool:
        """
        购物车「購入手続き」后偶发的お届け先选择页。
        特征：进度/标题含お届け先，有「次へ」，且尚无「注文を確定する」。
        """
        # 已到确认页则不是中间页
        for el in self._find_elements_now(
            driver,
            By.CSS_SELECTOR,
            'button[aria-label="注文を確定する"], .commit-order-button button',
        ):
            try:
                if el.is_displayed():
                    return False
            except Exception:
                continue

        has_next = False
        for el in self._find_next_step_buttons(driver):
            try:
                if el.is_displayed() and el.is_enabled():
                    has_next = True
                    break
            except Exception:
                continue
        if not has_next:
            return False

        try:
            src = driver.page_source or ""
        except Exception:
            src = ""
        markers = (
            "お届け先",
            "デフォルトのお届け先",
            "別の住所に送る",
            "別のお届け先に送る",
            "お支払い・お届け",
        )
        if any(m in src for m in markers):
            return True

        # 标题节点兜底
        for el in self._find_elements_now(
            driver,
            By.XPATH,
            "//*[self::h1 or self::h2 or self::h3]"
            "[contains(normalize-space(.),'お届け先')]",
        ):
            try:
                if el.is_displayed():
                    return True
            except Exception:
                continue
        return False

    def _find_next_step_buttons(self, driver):
        found = []
        cfg_sel = (self.ri_cfg.get("delivery_next_button_css") or "").strip()
        if cfg_sel:
            try:
                found.extend(self._find_elements_now(driver, By.CSS_SELECTOR, cfg_sel))
            except Exception:
                pass
        for sel in (
            'button[aria-label="次へ"]',
            'button[aria-label*="次へ"]',
        ):
            try:
                found.extend(self._find_elements_now(driver, By.CSS_SELECTOR, sel))
            except Exception:
                pass
        # 文案精确匹配，避免误点其它按钮
        for el in self._find_elements_now(
            driver,
            By.XPATH,
            "//button[normalize-space(.)='次へ' or @aria-label='次へ' "
            "or normalize-space(.)='次へ進む']",
        ):
            found.append(el)
        # 去重
        uniq = []
        seen = set()
        for el in found:
            try:
                key = id(el)
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(el)
            except Exception:
                continue
        return uniq

    def _pass_delivery_address_step_if_present(self, driver) -> bool:
        """
        若当前为お届け先中间页，点击「次へ」进入注文確認。
        默认地址通常已勾选，无需改选。返回是否点击过。
        """
        max_rounds = int(self.ri_cfg.get("delivery_address_step_max_rounds", 2) or 2)
        clicked_any = False
        for round_idx in range(max(1, max_rounds)):
            if not self._is_delivery_address_step(driver):
                if round_idx == 0:
                    self.logger.debug("乐天市场：未出现お届け先中间页，继续确认流程")
                break

            next_btn = None
            for el in self._find_next_step_buttons(driver):
                try:
                    if el.is_displayed() and el.is_enabled():
                        text = ((el.text or "") + " " + (el.get_attribute("aria-label") or "")).strip()
                        if "次へ" in text or (el.get_attribute("aria-label") or "").strip() == "次へ":
                            next_btn = el
                            break
                except Exception:
                    continue
            if next_btn is None:
                # 严格按可见红钮文案再找一次
                for el in self._find_elements_now(
                    driver,
                    By.XPATH,
                    "//button[contains(normalize-space(.),'次へ')]",
                ):
                    try:
                        if el.is_displayed() and el.is_enabled():
                            next_btn = el
                            break
                    except Exception:
                        continue
            if next_btn is None:
                self.logger.warning("乐天市场：检测到お届け先页但未找到「次へ」按钮")
                break

            self.logger.info(
                "乐天市场：检测到お届け先中间页，点击「次へ」（第 %s 次）",
                round_idx + 1,
            )
            self._random_pre_click_wait("お届け先次へ")
            try:
                driver.execute_script("arguments[0].click();", next_btn)
            except Exception:
                next_btn.click()
            clicked_any = True
            time.sleep(
                float(self.ri_cfg.get("wait_after_delivery_next_seconds", 3) or 3)
            )
            self._dismiss_interruptions(driver, timeout=2.0)

        return clicked_any

    def _is_order_success_page(self, driver) -> bool:
        """
        确认页正文里也会出现「注文完了」（积分/延保说明），不能单独用该词判断成功。
        成功页以感谢文案或「注文番号 + 注文番号样式单号」为准，且确认按钮应已消失。
        """
        src = driver.page_source or ""
        success_kw = (
            self.ri_cfg.get("success_page_text") or "ご注文ありがとうございます"
        ).strip()
        if success_kw and success_kw in src:
            return True

        # 仍停在确认页：可见「注文を確定する」则未完成。
        for el in self._find_elements_now(
            driver,
            By.CSS_SELECTOR,
            'button[aria-label="注文を確定する"], .commit-order-button button',
        ):
            try:
                if el.is_displayed():
                    return False
            except Exception:
                continue

        if "注文番号" not in src:
            return False
        return bool(_ORDER_NO_RE.search(src))

    @staticmethod
    def _extract_purchase_no(driver) -> str:
        try:
            m = _ORDER_NO_RE.search(driver.page_source or "")
            if m:
                return m.group(0)
        except Exception:
            pass
        return ""

    def _build_books_handoff_config(self) -> Dict[str, Any]:
        """
        市场站点转交书店流程时的配置：
        复用同一浏览器会话；回传信用卡标识沿用市场单（GroupId 246）。
        """
        cfg = dict(self.config)
        rb = dict(cfg.get("rakuten_books") or {})
        rb.setdefault("store_name", "乐天书店")
        rb.setdefault(
            "purchase_url_template",
            "https://books.rakuten.co.jp/mypage/delivery/status?order_number={purchase_no}",
        )
        ichiba_card = (
            (self.ri_cfg.get("add_no_credit_card") or "").strip()
            or ((cfg.get("payment") or {}).get("add_no_credit_card") or "").strip()
            or "8828"
        )
        # 市场拉单转交时强制用市场回传卡名，避免误用独立书店站的 rakuten_books 标识
        rb["add_no_credit_card"] = ichiba_card
        cfg["rakuten_books"] = rb
        return cfg

    def _handoff_to_books_processor(
        self, order: Dict[str, Any], products: List[Dict[str, Any]]
    ) -> Tuple[bool, Dict[str, Any]]:
        """全部为书店商品时，转交乐天书店全流程（同账号、同浏览器）。"""
        from src.order.rakuten_books_processor import (
            RakutenBooksOrderProcessor,
            normalize_rakuten_books_product_url,
        )

        normalized: List[Dict[str, Any]] = []
        for p in products:
            row = dict(p)
            row["url"] = normalize_rakuten_books_product_url(str(p.get("url") or ""))
            normalized.append(row)
        merged = self._merge_duplicate_products(normalized)
        order_id = order.get("order_id", "未知")
        self.logger.info(
            "乐天市场：订单 %s 识别为乐天书店商品，转交书店流程（同浏览器，合并后 %s 种）",
            order_id,
            len(merged),
        )
        order2 = dict(order)
        order2["products"] = merged
        books = RakutenBooksOrderProcessor(
            self._build_books_handoff_config(), self.browser_manager
        )
        return books.process_order(order2)

    def process_order(self, order: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        order_id = order.get("order_id", "未知")
        products: List[Dict[str, Any]] = order.get("products") or []
        self.logger.info("乐天市场：开始处理订单 %s，商品数 %s", order_id, len(products))
        if not products:
            return False, self._make_summary(order, failure_reason="订单无商品")

        url_products = [
            p for p in products if (p.get("url") or "").strip()
        ]
        books_products = [
            p
            for p in url_products
            if is_rakuten_books_product_url((p.get("url") or "").strip())
            or is_rakuten_books_product_url(
                self._direct_product_url((p.get("url") or "").strip())
            )
        ]
        non_books_products = [
            p
            for p in url_products
            if p not in books_products
        ]

        # 整单均为书店商品：同账号转交书店全流程
        if books_products and not non_books_products:
            return self._handoff_to_books_processor(order, books_products)

        # 市场+书店混单：购物车体系不同，无法一单自动完成
        if books_products and non_books_products:
            msg = (
                "乐天市场订单同时含市场商品与书店商品，无法自动混下，请人工拆单处理。"
            )
            details = [
                "书店链接: %s" % ((p.get("url") or "").strip())
                for p in books_products[:5]
            ]
            self.logger.warning("%s %s", msg, details)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg] + details,
                    user_id=order.get("user_id"),
                    extra="乐天市场 adapter=rakuten，已跳过本单并继续后续订单。",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order, failure_reason=msg, runner_pause_requested=False
            )

        cart_products: List[Dict[str, Any]] = []
        for p in products:
            if not (p.get("url") or "").strip():
                self.logger.warning(
                    "乐天市场：跳过无 GoodsUrl 的商品 goods_no=%s",
                    p.get("goods_no"),
                )
                continue
            cart_products.append(p)
        if not cart_products:
            return False, self._make_summary(order, failure_reason="订单无有效商品链接（GoodsUrl）")

        if not str(order.get("secret") or "").strip():
            self.logger.warning(
                "乐天市场：订单 %s 无 secret，后续回调验签可能失败",
                order_id,
            )

        driver = self.browser_manager.get_driver()
        use_curl = (self.config.get("order_api") or {}).get("use_curl_for_order_api", True)

        try:
            driver = self.browser_manager.ensure_alive(restart_if_dead=True)
        except Exception as e:
            msg = "浏览器窗口不可用，无法继续: %s" % e
            self.logger.error("乐天市场：%s order=%s", msg, order_id)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="乐天市场：Chrome 窗口已关闭，请检查浏览器后重试。",
                )
            except Exception:
                pass
            return False, self._make_summary(order, failure_reason=msg)

        try:
            self._ensure_rakuten_session(resume_url=self._cart_url())
        except RakutenLoginError as e:
            msg = "乐天登录失败: %s" % e
            self.logger.error("乐天市场：%s order=%s", msg, order_id)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="乐天市场：请检查 login.password 或手动完成登录后重试。",
                )
            except Exception:
                pass
            return False, self._make_summary(order, failure_reason=msg)

        try:
            self._clear_cart(driver)
        except Exception as e:
            msg = "清空购物车失败: %s" % e
            self.logger.error("乐天市场：%s order=%s", msg, order_id)
            return False, self._make_summary(order, failure_reason=msg)

        verified_products: List[Dict[str, Any]] = []
        # 先按 URL+规格合并，详情页一次写入总数量再加购（25 行重复可降到约 6 次）。
        add_groups = self._merge_duplicate_products(cart_products)
        for idx, product in enumerate(add_groups, 1):
            purl = (product.get("url") or "").strip()
            qty = int(product.get("quantity") or 1)
            source_lines: List[Dict[str, Any]] = list(
                product.get("_source_lines") or [product]
            )
            for line in source_lines:
                gid, gno = self._api_goods_id_and_no(line)
                if not gid or not gno:
                    msg = (
                        "订单商品缺少 GoodsId/GoodsNo（getOrderListSimple List） "
                        "goods_id=%r goods_no=%r"
                        % (gid, gno)
                    )
                    self.logger.error("乐天市场：%s order=%s", msg, order_id)
                    try:
                        self.feishu_notifier.notify_order_issue(
                            str(order_id),
                            [msg],
                            user_id=order.get("user_id"),
                            extra="乐天市场：接口 List 缺 GoodsNo，已跳过本单。",
                        )
                    except Exception:
                        pass
                    return False, self._make_summary(order, failure_reason=msg)

            try:
                self.logger.info(
                    "乐天市场：加购 %s/%s（合并数量=%s，接口行=%s） %s",
                    idx,
                    len(add_groups),
                    qty,
                    len(source_lines),
                    purl,
                )
                expected_products = verified_products + [product]
                self._add_product_to_cart(driver, product, expected_products)
                verified_products = expected_products
            except RakutenBooksHandoffNeeded as e:
                # 尚未成功加购任何市场商品时，整单转交书店流程（回调仍用当前订单）
                if not verified_products:
                    self.logger.warning(
                        "乐天市场：加购中发现书店商品（%s），尚未加购市场商品，转交书店流程",
                        e.url or purl,
                    )
                    return self._handoff_to_books_processor(order, cart_products)
                msg = (
                    "乐天市场订单加购中途出现书店商品，且已有市场商品在购物车，"
                    "无法自动混下，请人工拆单。书店链接: %s" % (e.url or purl)
                )
                self.logger.error(msg)
                try:
                    self.feishu_notifier.notify_order_issue(
                        str(order_id),
                        [msg],
                        user_id=order.get("user_id"),
                        extra="乐天市场 adapter=rakuten，已跳过本单并继续后续订单。",
                    )
                except Exception:
                    pass
                return False, self._make_summary(
                    order, failure_reason=msg, runner_pause_requested=False
                )
            except Exception as e:
                err_text = str(e)
                if "sold-out" in err_text.lower() or "purchasecondition=sold" in err_text.lower():
                    msg = "商品已售罄，无法加购: %s" % purl
                elif "乐天书店" in err_text and not verified_products:
                    # 兼容旧异常文案：整单转交
                    self.logger.warning(
                        "乐天市场：捕获书店相关失败且未加购，转交书店流程: %s", err_text
                    )
                    return self._handoff_to_books_processor(order, cart_products)
                else:
                    msg = "加购失败: %s url=%s" % (err_text, purl)
                self.logger.error(msg)
                try:
                    self.feishu_notifier.notify_order_issue(
                        str(order_id), [msg], user_id=order.get("user_id"), extra="乐天市场"
                    )
                except Exception:
                    pass
                return False, self._make_summary(order, failure_reason=msg)

            # 后端校验：接口 List 每一行必须单独调用 addedCartCallbackSimple，不可省略。
            for line in source_lines:
                gid, gno = self._api_goods_id_and_no(line)
                line_qty = max(1, int(line.get("quantity") or 1))
                callback_product: Dict[str, Any] = {
                    "goods_id": gid,
                    "goods_no": gno,
                    "shop_id": self._store_name(),
                    "quantity": line_qty,
                }
                try:
                    ok_cb = send_added_cart_callback(
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
                    msgs = [
                        "乐天市场：addedCartCallbackSimple 未成功（GoodsNo=%s）" % gno
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
                            extra="乐天市场加购回调失败",
                        )
                    except Exception:
                        pass
                    return False, self._make_summary(
                        order, failure_reason="addedCartCallbackSimple 未成功"
                    )

        # 在购物车页取总金额/运费并 checkCart（避免确认页电话号误解析成运费）
        try:
            self._ensure_cart_page(driver, quick=False)
            self._dismiss_interruptions(driver, timeout=2.0)
            goods_list, total, goods_fee, operate_fee = self._build_check_cart_from_cart(
                cart_products, driver
            )
        except Exception as e:
            msg = "购物车组装结算校验失败: %s" % e
            self.logger.error("乐天市场：%s order=%s", msg, order_id)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="乐天市场：请在购物车确认小计/送料后重试。",
                )
            except Exception:
                pass
            return False, self._make_summary(order, failure_reason=msg)

        if not goods_list:
            return False, self._make_summary(
                order,
                failure_reason="无法组装结算校验商品列表（请确认接口 List 含 goods_no）",
            )

        shot_path = None
        try:
            shot_path = take_full_page_screenshot(driver)
            screen_url = upload_screenshot_get_url(shot_path, self.config)
            if not screen_url:
                return False, self._make_summary(order, failure_reason="截图上传失败")
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
        finally:
            if shot_path:
                try:
                    import os

                    os.remove(shot_path)
                except Exception:
                    pass

        if not ok_chk:
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [chk_err or "checkCartGoodsSimple 失败"],
                    user_id=order.get("user_id"),
                    extra="乐天市场结算校验失败（购物车金额）",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=chk_err or "checkCartGoodsSimple 失败",
                check_cart_requested=True,
                check_cart_response=(chk_raw or "")[:500],
            )

        try:
            self._shop_checkout(driver)
        except Exception as e:
            msg = "进入店铺结算失败: %s" % e
            self.logger.error("乐天市场：%s order=%s", msg, order_id)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="乐天市场：购物车无法进入購入手続き（常见：限购导致按钮禁用）。",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=msg,
                check_cart_requested=True,
                check_cart_response="ok",
            )

        self._dismiss_interruptions(driver, timeout=3.0)
        try:
            self._pass_delivery_address_step_if_present(driver)
        except Exception as e:
            self.logger.warning("乐天市场：处理お届け先中间页异常（继续）: %s", e)

        commit_sel = (
            self.ri_cfg.get("commit_order_button_css")
            or '.commit-order-button button[aria-label="注文を確定する"]'
        ).strip()
        try:
            # 部分店铺在点確定前就会弹出需选第一项并确认的 modal
            self._handle_confirm_page_option_modal(driver, timeout=3.0)
            self._random_pre_click_wait("注文を確定する")
            commit_btn = WebDriverWait(driver, 25).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, commit_sel))
            )
            driver.execute_script("arguments[0].click();", commit_btn)
        except Exception as e:
            msg = "点击注文確定失败: %s" % e
            self.logger.error("乐天市场：%s order=%s", msg, order_id)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="乐天市场：确认页无法点击注文を確定する。",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=msg,
                check_cart_requested=True,
                check_cart_response="ok",
            )

        sec = int(self.ri_cfg.get("success_page_wait_seconds", 120))
        deadline = time.time() + max(10, sec)
        ok_page = False
        while time.time() < deadline:
            try:
                # 点確定后常见：店铺注意事项 / 选项弹窗 → 选第一项并确认
                self._handle_confirm_page_option_modal(driver, timeout=2.0)
            except Exception:
                pass
            try:
                if self._is_order_success_page(driver):
                    ok_page = True
                    break
            except Exception:
                pass
            time.sleep(1.0)

        if not ok_page:
            msg = (
                "乐天市场：超时未检测到成功页（需「ご注文ありがとうございます」或注文番号，"
                "且确认页「注文を確定する」已消失），可能 3DS 或页面异常。URL: %s"
                % (driver.current_url or "")
            )
            self.logger.error(msg)
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [msg],
                    user_id=order.get("user_id"),
                    extra="本单已跳过，调度将继续处理后续订单（非暂停）。",
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

        purchase_no = self._extract_purchase_no(driver)
        if not purchase_no:
            msg = "乐天市场：成功页未解析到注文番号"
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id), [msg], user_id=order.get("user_id"), extra="乐天市场"
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=msg,
                check_cart_requested=True,
                check_cart_response="ok",
            )

        shop_id = purchase_no.split("-", 1)[0] if purchase_no else ""
        tpl = (self.ri_cfg.get("purchase_url_template") or "").strip() or (
            (self.config.get("order_api") or {}).get("purchase_url_template") or ""
        ).strip()
        if not tpl:
            tpl = (
                "https://order.my.rakuten.co.jp/purchase-history/"
                "?shop_id={shop_id}&order_number={purchase_no}"
                "&act=detail_page_view&source=order_list_search"
            )
        detail_url = tpl.format(shop_id=shop_id, purchase_no=purchase_no)
        purchase_nobs = [{"no": purchase_no, "url": detail_url}]

        ok_add, add_err, add_raw = send_add_no_callback(
            order,
            purchase_nobs,
            credit_card=self._credit_card_label(),
            config=self.config,
            use_curl=use_curl,
        )
        if not ok_add:
            try:
                self.feishu_notifier.notify_order_issue(
                    str(order_id),
                    [add_err or "addNoCallbackSimple 失败"],
                    user_id=order.get("user_id"),
                    extra="乐天市场",
                )
            except Exception:
                pass
            return False, self._make_summary(
                order,
                failure_reason=add_err or "addNoCallbackSimple 失败",
                check_cart_requested=True,
                check_cart_response="ok",
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
            goods_no_list = [{"no": purchase_no, "price": total or goods_fee, "num": 1}]
        try:
            self._navigate(driver, detail_url)
            time.sleep(float(self.ri_cfg.get("wait_after_detail_load_seconds", 3)))
            shot2 = take_full_page_screenshot(driver)
            detail_shot_url = upload_screenshot_get_url(shot2, self.config)
            ok_u, uerr = send_update_goods_no_callback(
                order,
                purchase_no,
                goods_no_list,
                detail_shot_url or "",
                self._store_name(),
                self.config,
                use_curl=use_curl,
            )
            if not ok_u:
                update_errors.append(uerr or "updateGoodsNoCallback 失败")
        except Exception as e:
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
                    update_errors,
                    user_id=order.get("user_id"),
                    extra="乐天市场分单回调异常",
                )
            except Exception:
                pass

        return True, self._make_summary(
            order,
            success=True,
            payment_method="rakuten_creditcard",
            check_cart_requested=True,
            check_cart_response="ok",
            add_no_requested=True,
            add_no_response=(add_raw or "")[:500],
            update_errors=update_errors,
        )
