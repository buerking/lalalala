"""
支付处理器
"""

from typing import Dict, Any, Optional, List
import time
import logging
import re
from datetime import datetime, timedelta
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from PIL import Image
import io

from src.utils.logger import LoggerMixin
from src.utils.retry import retry
from src.notification.feishu_notifier import FeishuNotifier
from src.payment.confirm_page_verifier import (
    take_full_page_screenshot,
    upload_screenshot_get_url,
    parse_confirm_summary,
    check_cart_goods_simple,
)
from src.order.add_no_callback import send_add_no_callback
from src.order.update_goods_no_callback import send_update_goods_no_callback
from src.auth.surugaya_session import SurugayaLoginError, SurugayaSessionGuard


class PaymentHandler(LoggerMixin):
    """支付处理器"""
    
    def __init__(self, browser_manager, config: Dict[str, Any]):
        """
        初始化支付处理器
        
        Args:
            browser_manager: 浏览器管理器实例
            config: 配置字典
        """
        self.browser_manager = browser_manager
        self.config = config
        self.session_guard = None
        if SurugayaSessionGuard.is_enabled(config):
            self.session_guard = SurugayaSessionGuard(browser_manager, config)
        self.payment_config = config.get('payment', {})
        self.cart_config = config.get('cart_page', {})
        self.order_page_config = config.get('order_page', {})
        self.order_confirm_page_config = config.get('order_confirm_page', {})
        self.feishu_notifier = FeishuNotifier(config)
        # PayPay：扫码时间段（为空表示不限制）、提前通知后等待秒数
        self.paypay_scan_time_ranges: List[str] = self.payment_config.get('paypay_scan_time_ranges') or []
        self.paypay_advance_notify_wait_seconds: int = self.payment_config.get('paypay_advance_notify_wait_seconds', 60)
    
    def _is_in_paypay_scan_window(self) -> bool:
        """
        判断当前时间是否在配置的 PayPay 扫码时间段内。
        配置格式：["09:00-12:00", "14:00-22:00"]，24 小时制；支持跨日如 "22:00-06:00"。
        若 paypay_scan_time_ranges 为空或未配置，视为不限制，返回 True。
        """
        if not self.paypay_scan_time_ranges:
            return True
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute  # 0..1439

        def parse_time(s: str) -> int:
            s = s.strip()
            parts = s.split(':')
            h = int(parts[0]) if parts else 0
            m = int(parts[1]) if len(parts) > 1 else 0
            return h * 60 + m

        for range_str in self.paypay_scan_time_ranges:
            range_str = (range_str or "").strip()
            if not range_str or "-" not in range_str:
                continue
            start_s, end_s = range_str.split("-", 1)
            start_m = parse_time(start_s)
            end_m = parse_time(end_s)
            if start_m <= end_m:
                if start_m <= current_minutes <= end_m:
                    return True
            else:
                # 跨日：如 22:00-06:00 即 22:00~24:00 或 0:00~06:00
                if current_minutes >= start_m or current_minutes <= end_m:
                    return True
        return False
    
    @retry(max_attempts=3, delay=2.0)
    def click_pay_button(self):
        """点击购物车页「注文画面に進む」按钮，进入订单/支付流程"""
        driver = self.browser_manager.get_driver()
        pay_button_selector = self.cart_config.get('pay_button_selector', '')
        cart_url = self.cart_config.get('url', '')
        
        if not pay_button_selector:
            raise ValueError("支付按钮选择器未配置，请在 config.yaml 的 cart_page.pay_button_selector 中配置")
        
        try:
            if self.session_guard:
                self.session_guard.ensure_logged_in(resume_url=cart_url or None)
            pay_button = self.browser_manager.wait_for_clickable(
                By.CSS_SELECTOR, pay_button_selector, timeout=10
            )
            self.logger.info("点击「注文画面に進む」按钮")
            driver.execute_script("arguments[0].click();", pay_button)
            time.sleep(self.cart_config.get('wait_after_load_seconds', 3))
            if self.session_guard and self.session_guard.is_login_page(driver):
                self.logger.warning("进入结算时出现登录页，自动登录后重新打开购物车并再点一次")
                self.session_guard.ensure_logged_in(resume_url=cart_url or None)
                if cart_url:
                    driver.get(cart_url)
                    time.sleep(self.cart_config.get('wait_after_load_seconds', 3))
                    self.session_guard.ensure_logged_in(resume_url=cart_url)
                pay_button = self.browser_manager.wait_for_clickable(
                    By.CSS_SELECTOR, pay_button_selector, timeout=10
                )
                driver.execute_script("arguments[0].click();", pay_button)
                time.sleep(self.cart_config.get('wait_after_load_seconds', 3))
                if self.session_guard.is_login_page(driver):
                    raise SurugayaLoginError("进入结算后仍被重定向到登录页")
        except SurugayaLoginError:
            raise
        except Exception as e:
            self.logger.error(f"点击支付按钮失败: {e}")
            raise
    
    def select_payment_method(self, order: Dict[str, Any]) -> str:
        """
        在订单确认页（cargo/order1）选择支付方式。

        仅按页面实际情况做**固定优先级**（不再根据 tenpo_cd / 预售等订单字段分支）：
        1. **0 円代金引換**：文案判断为手数料 0 円，且代引单选可用 → 选货到付款。
        2. **PayPal**：单选可用则选 PayPal。
        3. **PayPay**：否则选 PayPay（分店单往往不提供代引/PayPal，自然落在此项）。

        对方系统逻辑如此；分店并不是「有 tenpo_cd 就写死 PayPay」，而是页面上其它方式不可用。
        """
        driver = self.browser_manager.get_driver()
        order_id = str((order or {}).get("order_id") or "").strip()
        cash_sel = self.order_page_config.get('cash_delivery_container', 'dl#cash_delivery_custome')
        cash_radio_sel = self.order_page_config.get('cash_delivery_radio', 'input#edit-payment-cash-delivery')
        paypay_radio_sel = self.order_page_config.get('paypay_radio', 'input#edit-payment-paypay')
        paypal_radio_sel = self.order_page_config.get('paypal_radio', 'input#edit-payment-paypal')
        cash_radio_id = self._radio_id_from_selector(cash_radio_sel) or "edit-payment-cash-delivery"
        paypay_radio_id = self._radio_id_from_selector(paypay_radio_sel) or "edit-payment-paypay"
        paypal_radio_id = self._radio_id_from_selector(paypal_radio_sel) or "edit-payment-paypal"

        # ---------- 1. 判断：代金引換是否存在、手续费是否 0 円 ----------
        has_cash_delivery = False
        cash_fee_is_zero = False
        fee_text = ""
        try:
            container = driver.find_element(By.CSS_SELECTOR, cash_sel)
            has_cash_delivery = True
            fee_text = (container.text or '').strip()
            has_zero_mention = '0円' in fee_text and '手数料' in fee_text
            has_nonzero_fee = bool(re.search(r'[1-9]\d*円', fee_text))
            cash_fee_is_zero = has_zero_mention and not has_nonzero_fee
        except Exception as e:
            self.logger.debug("代金引換区域未找到或读取手续费失败: %s", e)

        paypal_ok = self._is_payment_radio_usable(driver, paypal_radio_sel, paypal_radio_id)
        paypay_ok = self._is_payment_radio_usable(driver, paypay_radio_sel, paypay_radio_id)
        cash_radio_ok = self._is_payment_radio_usable(driver, cash_radio_sel, cash_radio_id)

        self.logger.info(
            "[支付方式] 订单=%s | 优先级: 0円代引 > PayPal > PayPay",
            order_id or "(未知)",
        )
        self.logger.info(
            "[支付方式] 代金引換区域=%s, 文案0円=%s | 单选可用: 代引=%s, PayPal=%s, PayPay=%s",
            has_cash_delivery,
            cash_fee_is_zero,
            cash_radio_ok,
            paypal_ok,
            paypay_ok,
        )

        # ---------- 2. 固定优先级：0円代引 > PayPal > PayPay ----------
        if has_cash_delivery and cash_fee_is_zero and cash_radio_ok:
            chosen = "cash"
            reason = "代金引換手数料 0 円且代引可选"
        elif paypal_ok:
            chosen = "paypal"
            reason = "PayPal 可选"
        elif paypay_ok:
            chosen = "paypay"
            reason = "PayPay 可选（代引不满足或 PayPal 不可用时的回退）"
        else:
            raise RuntimeError(
                "未找到可用的支付方式（0円代引 / PayPal / PayPay 均不可用），订单=%s"
                % (order_id or "?")
            )

        self.logger.info("[支付方式] 判断结果: 选择=%s, 原因=%s", chosen, reason)

        # ---------- 3. 根据「选择」点击对应 radio ----------
        clicked = False
        if chosen == "cash":
            clicked = self._click_payment_radio(driver, cash_radio_sel, radio_id=cash_radio_id)
        elif chosen == "paypay":
            clicked = self._click_payment_radio(driver, paypay_radio_sel, radio_id=paypay_radio_id)
        else:
            clicked = self._click_payment_radio(driver, paypal_radio_sel, radio_id=paypal_radio_id)

        if not clicked:
            raise RuntimeError("无法勾选支付方式（选择=%s）：对应 radio 未找到或不可点击" % chosen)

        time.sleep(0.5)
        return chosen

    def _is_payment_radio_usable(self, driver, input_selector: str, radio_id: str) -> bool:
        """
        判断支付方式单选是否在页面上可用（可见且 input 未 disabled，或可见的 label[for]）。
        用于按「0円代引 > PayPal > PayPay」优先级时，只选择页面上真实存在的项。
        """
        if not input_selector:
            return False
        try:
            for el in self.browser_manager.find_elements_now(By.CSS_SELECTOR, input_selector):
                try:
                    if not el.is_displayed():
                        continue
                    tag = (el.tag_name or "").lower()
                    if tag == "input" and not el.is_enabled():
                        continue
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        if not radio_id:
            return False
        try:
            label_sel = "label[for='%s']" % radio_id
            for el in self.browser_manager.find_elements_now(By.CSS_SELECTOR, label_sel):
                try:
                    if el.is_displayed():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    @staticmethod
    def _radio_id_from_selector(selector: str) -> str:
        """从 input#edit-payment-xxx 中解析出 id，用于 label[for=...]"""
        if not selector:
            return ""
        if "#" in selector:
            return selector.split("#")[-1].strip()
        return selector.strip()
    
    def _click_payment_radio(self, driver, input_selector: str, radio_id: str) -> bool:
        """尝试点击支付方式单选框（先 input，再 label[for=id]），成功返回 True"""
        try:
            el = self.browser_manager.wait_for_clickable(By.CSS_SELECTOR, input_selector, timeout=5)
            driver.execute_script("arguments[0].click();", el)
            self.logger.info("[支付方式] 已点击单选框: %s", input_selector)
            return True
        except TimeoutException:
            try:
                label_sel = "label[for='%s']" % radio_id
                el = self.browser_manager.wait_for_clickable(By.CSS_SELECTOR, label_sel, timeout=3)
                driver.execute_script("arguments[0].click();", el)
                self.logger.info("[支付方式] 已通过 label 点击: %s", label_sel)
                return True
            except Exception as e2:
                self.logger.warning("[支付方式] 单选框不可用 %s: %s", input_selector, e2)
                return False
        except Exception as e:
            self.logger.warning("[支付方式] 点击单选框失败 %s: %s", input_selector, e)
            return False
    
    def click_confirm_order_button(self) -> None:
        """点击「ご注文内容の確認へ」进入订单确认页"""
        driver = self.browser_manager.get_driver()
        btn_sel = self.order_page_config.get('confirm_order_button', 'div#next_step input#edit-submit')
        self.logger.info("[支付方式] 点击确认: 查找按钮 selector=%s", btn_sel)
        btn = self.browser_manager.wait_for_clickable(By.CSS_SELECTOR, btn_sel, timeout=10)
        self.logger.info("[支付方式] 点击「ご注文内容の確認へ」")
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(self.order_page_config.get('wait_after_load_seconds', 3))

    def _is_order_completed_page(self) -> bool:
        """判断是否进入订单成功页面（ご注文完了）。多条件兜底，避免漏判。"""
        driver = self.browser_manager.get_driver()
        try:
            h2 = driver.find_element(By.CSS_SELECTOR, "h2.cart_title")
            title = (h2.text or "").strip()
            if "ご注文完了" in title:
                return True
        except Exception:
            pass
        try:
            thanks = driver.find_element(By.CSS_SELECTOR, "div#thanks")
            if thanks.is_displayed():
                return True
        except Exception:
            pass
        try:
            body_text = (driver.find_element(By.TAG_NAME, "body").text or "")
            if "ご注文完了" in body_text or "ご注文ありがとうございました" in body_text:
                return True
        except Exception:
            pass
        return False

    def _extract_purchase_no(self) -> str:
        """从成功页提取单个骏河屋取引番号（如 S2603065490）；有多分单时取第一个。"""
        all_nos = self._extract_all_purchase_nos()
        return all_nos[0] if all_nos else ""

    def _extract_all_purchase_nos(self) -> list:
        """
        从成功页提取全部取引番号（分单可能多个）。
        支持 S/M/Y 开头（如 S2603065490、M2603100723、Y2603133781），并提供多种兜底方案。
        """
        driver = self.browser_manager.get_driver()
        seen = set()
        result = []
        # 取引番号格式：任意大写字母开头 + 数字（兼容未来可能出现的新前缀）
        pattern = re.compile(r"[A-Z]\d+")
        try:
            # 1) 从 dataLayer 的 transaction_id 提取（与页面展示一致、顺序稳定）
            try:
                html = driver.page_source or ""
                for m in re.finditer(r"transaction_id\s*['\"]?\s*:\s*['\"]?([A-Z]\d+)", html, re.I):
                    t = m.group(1).strip()
                    if t and t not in seen:
                        seen.add(t)
                        result.append(t)
            except Exception:
                pass
            # 2) 多个 p.torihiki_num 或 span 内文本
            if not result:
                blocks = driver.find_elements(By.CSS_SELECTOR, "p.torihiki_num")
                for p in blocks:
                    spans = p.find_elements(By.CSS_SELECTOR, "span")
                    for span in spans:
                        t = (span.text or "").strip()
                        if t and pattern.fullmatch(t) and t not in seen:
                            seen.add(t)
                            result.append(t)
                # 同一 p 的全文（取引番号はXXXです）
                for p in blocks:
                    text = (p.text or "") or ""
                    for m in pattern.finditer(text):
                        t = m.group(0)
                        if t not in seen:
                            seen.add(t)
                            result.append(t)
            # 3) 链接中的 trade_code 参数（例如详情页 URL 里的 trade_code=Y2603133781）
            if not result:
                try:
                    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='trade_code=']")
                    for a in anchors:
                        href = (a.get_attribute("href") or "").strip()
                        for m in pattern.finditer(href):
                            t = m.group(0)
                            if t not in seen:
                                seen.add(t)
                                result.append(t)
                except Exception:
                    pass
            # 4) 整页正文兜底
            if not result:
                body = driver.find_element(By.TAG_NAME, "body")
                text = body.text or ""
                for m in pattern.finditer(text):
                    t = m.group(0)
                    if t not in seen:
                        seen.add(t)
                        result.append(t)
        except Exception:
            pass
        return result

    def _parse_detail_table_goods(self, driver) -> list:
        """从骏河屋订单详情页 table.mgnT15.paddTbl 解析商品：品番(no)、単価(price)、数量(num)。跳过表头与合计行。"""
        goods_list = []
        try:
            table = driver.find_element(By.CSS_SELECTOR, "table.mgnT15.paddTbl")
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            for tr in rows:
                # 表头行通常是 th，不参与解析
                try:
                    if tr.find_elements(By.TAG_NAME, "th"):
                        continue
                except Exception:
                    pass
                tds = tr.find_elements(By.TAG_NAME, "td")
                if len(tds) < 6:
                    continue
                first_text = (tds[0].text or "").strip()
                # 商品行：品番可能是字母数字（如 ZHOTO89849 / BO2857627n），不应限制为纯数字
                # 合计/运费等行通常为 colspan，且第一个 td 文案为「手数料」「送料・通信販売手数料」「総合計」等
                if not first_text:
                    continue
                if any(k in first_text for k in ("手数料", "送料", "通信販売手数料", "総合計", "商品代金")):
                    continue
                no = first_text
                price_text = (tds[4].text or "").strip()
                num_text = (tds[5].text or "").strip()
                price = int(re.sub(r"[^\d]", "", price_text)) if price_text else 0
                num = int(re.sub(r"[^\d]", "", num_text)) if num_text else 0
                if no and (price or num):
                    goods_list.append({"no": no, "price": price, "num": num})
        except Exception as e:
            self.logger.warning("[支付流程] 解析详情页商品表失败: %s", e)
        return goods_list

    def _build_purchase_url(self, purchase_no: str) -> str:
        """根据配置拼接商家订单查看 URL。"""
        tpl = ((self.config.get("order_api") or {}).get("purchase_url_template") or "").strip()
        if not tpl:
            return ""
        try:
            return tpl.format(purchase_no=purchase_no)
        except Exception:
            return ""

    def _poll_until_completed(
        self,
        order_id: str,
        timeout_seconds: int = 1800,
        interval_seconds: int = 30,
        payment_method: str = "",
    ) -> bool:
        """每 interval_seconds 检测一次是否进入成功页，最长 timeout_seconds。不刷新页面，避免已到注文完了页被刷掉。"""
        start = datetime.now()
        while (datetime.now() - start).total_seconds() < timeout_seconds:
            if self._is_order_completed_page():
                self.logger.info("[支付流程] 已进入订单成功页面（ご注文完了）")
                return True
            elapsed = int((datetime.now() - start).total_seconds())
            self.logger.info("[支付流程] 未进入成功页，继续等待（%ss一次），已等待 %ss", interval_seconds, elapsed)
            time.sleep(interval_seconds)
        # 超时：按支付方式发飞书并处理
        if payment_method == "paypay":
            try:
                self.feishu_notifier.notify_order_issue(
                    order_id,
                    ["本单扫码支付未完成，请人工跟进。因系统锁定购物车，将于5分钟后继续下一单。"],
                    user_id=None,
                    extra="PayPay 5分钟内未进入注文完了，系统将等待5分钟后处理下一单。",
                )
            except Exception as e:
                self.logger.warning("[支付流程] PayPay 超时飞书提醒发送失败: %s", e)
            self.logger.info("[支付流程] PayPay 未完成，等待 5 分钟后跳过本单（购物车解锁）")
            time.sleep(300)
        else:
            try:
                self.feishu_notifier.notify_order_issue(
                    order_id,
                    [f"支付后等待超时：{timeout_seconds//60} 分钟内未进入「ご注文完了」页面，请人工确认。"],
                    user_id=None,
                    extra="系统已跳过当前订单，继续处理其他订单。",
                )
            except Exception as e:
                self.logger.warning("[支付流程] 超时飞书提醒发送失败: %s", e)
        return False

    def _send_page_screenshot_to_feishu(
        self,
        order_id: str,
        title: str,
        extra_hint: Optional[str] = None,
        use_paypay_scan_webhook: bool = False,
    ) -> None:
        """
        全页截图→上传→把截图以「可直接预览的图片形式」发送到飞书群。
        标题和正文均含订单ID，便于人工区分。
        """
        driver = self.browser_manager.get_driver()
        use_curl = (self.config.get('order_api') or {}).get('use_curl_for_order_api', True)
        try:
            path = take_full_page_screenshot(driver)
            url = upload_screenshot_get_url(path, self.config, use_curl=use_curl)
        except Exception as e:
            self.logger.warning("[支付流程] 截图上传失败: %s", e)
            url = ""
        content_lines = [
            f"**订单ID**: {order_id}",
            title,
        ]
        if extra_hint:
            content_lines.append(extra_hint)
        if url:
            # 既保留原始链接，又通过 markdown 内嵌图片，方便在飞书中直接预览
            content_lines.append(f"截图链接: {url}")
            content_lines.append("")
            content_lines.append(f"![页面截图]({url})")
        else:
            content_lines.append("截图上传失败或未返回 URL（请在浏览器中手动查看页面）")
        try:
            self.feishu_notifier.send_message(
                f"{title} - 订单{order_id}",
                "\n\n".join(content_lines),
                use_paypay_scan_webhook=use_paypay_scan_webhook,
            )
        except Exception as e:
            self.logger.warning("[支付流程] 飞书发送截图消息失败: %s", e)

    def _handle_paypal_middle_page(self) -> None:
        """PayPal 中间页：点击「注文の確認を続ける」按钮。"""
        driver = self.browser_manager.get_driver()
        # className 可能变化，优先用稳定属性定位
        selectors = [
            "button[data-id='payment-submit-btn']",
            "button[data-testid='submit-button-initial']",
            "button#payment-submit-btn",
        ]
        last_err = None
        for sel in selectors:
            try:
                btn = self.browser_manager.wait_for_clickable(By.CSS_SELECTOR, sel, timeout=10)
                self.logger.info("[支付流程] PayPal 中间页点击确认按钮: %s", sel)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                break
            except Exception as e:
                last_err = e
        else:
            raise RuntimeError(f"未找到 PayPal 中间页确认按钮: {last_err}")

        # 进入下一中间页时，可能还需要再次点击「ご注文確定」
        try:
            confirm_sel = "input#submitBtn"
            confirm_btn = self.browser_manager.wait_for_clickable(By.CSS_SELECTOR, confirm_sel, timeout=8)
            self.logger.info("[支付流程] PayPal 二次确认：点击「ご注文確定」(%s)", confirm_sel)
            driver.execute_script("arguments[0].click();", confirm_btn)
            time.sleep(2)
        except TimeoutException:
            # 未出现二次确认按钮则跳过
            pass

    def verify_confirm_page_and_check_cart(self, order: Dict[str, Any]) -> tuple:
        """
        在订单确认页（cargo/orderconfirm）解析合計金額、全页截图上传、调用 checkCartGoodsSimple。
        若 Success=false 或 Data=false 则停止后续操作。

        Returns:
            (True, None) 校验通过；(False, error_message) 校验失败
        """
        driver = self.browser_manager.get_driver()
        use_curl = (self.config.get('order_api') or {}).get('use_curl_for_order_api', True)
        confirm_config = self.order_confirm_page_config
        wait_sec = confirm_config.get('wait_after_load_seconds', 3)
        time.sleep(wait_sec)

        # 1. 解析页面 税込合計 / 送料+手数料+通信販売手数料 / 合計
        summary = parse_confirm_summary(driver, self.config)
        if not summary:
            return False, "解析订单确认页合計金額失败", ""
        total = summary["total"]
        goods_fee = summary["goods_fee"]
        operate_fee = summary["operate_fee"]
        self.logger.info(
            "[支付流程] 订单合计金额: 商品金额(税込合計)=%s円, 送料+手数料+通信费=%s円, 合計=%s円",
            goods_fee, operate_fee, total,
        )

        # 2. 全页截图并上传获取 URL
        screenshot_path = None
        try:
            screenshot_path = take_full_page_screenshot(driver)
            self.logger.info("[结算校验] 全页截图已保存: %s", screenshot_path)
        except Exception as e:
            self.logger.error("[结算校验] 全页截图失败: %s", e)
            return False, "全页截图失败: %s" % e, ""

        screenshot_url = ""
        try:
            screenshot_url = upload_screenshot_get_url(
                screenshot_path, self.config,
                use_curl=use_curl,
            )
        except Exception as e:
            self.logger.warning("[结算校验] 截图上传失败: %s", e)
        if not screenshot_url:
            self.logger.warning("[结算校验] 未获取到截图 URL，仍继续调用校验接口（ScreenShotUrl 为空）")

        # 3. 从确认页提取真实 GoodsList（可能分单多表），再调用 checkCartGoodsSimple
        goods_list_dom = []
        try:
            from src.payment.confirm_page_verifier import build_goods_list_from_confirm_page

            goods_list_dom = build_goods_list_from_confirm_page(driver, order)
        except Exception as e:
            self.logger.warning("[结算校验] 从确认页提取 GoodsList 异常，将回退使用订单原始数据: %s", e)
            goods_list_dom = []

        if goods_list_dom:
            try:
                # 只打印前若干条避免日志过长
                preview = json.dumps(goods_list_dom[:5], ensure_ascii=False)
                self.logger.info("[结算校验] 采用确认页真实 GoodsList，条数=%s，前5条=%s", len(goods_list_dom), preview)
            except Exception:
                self.logger.info("[结算校验] 采用确认页真实 GoodsList，条数=%s", len(goods_list_dom))
        else:
            self.logger.warning("[结算校验] 未从确认页提取到 GoodsList，将回退使用订单原始 GoodsList（可能与页面不一致）")

        # 4. 调用 checkCartGoodsSimple
        ok, err, raw_response = check_cart_goods_simple(
            order,
            total,
            goods_fee,
            operate_fee,
            screenshot_url,
            self.config,
            goods_list_override=(goods_list_dom if goods_list_dom else None),
            use_curl=use_curl,
        )
        if not ok:
            self.logger.error("[结算校验] checkCartGoodsSimple 未通过: %s", err)
            return False, err or "接口返回 Success=false 或 Data=false", (raw_response or "")
        self.logger.info("[结算校验] checkCartGoodsSimple 通过，继续执行")

        # 供 PayPay 对账留底等后续步骤使用
        self._last_confirm_amounts = {
            "total": int(total or 0),
            "goods_fee": int(goods_fee or 0),
            "operate_fee": int(operate_fee or 0),
        }
        return True, None, (raw_response or "")

    def click_submit_to_payment_button(self) -> None:
        """校验通过后点击进入下一步：先试「決済情報の入力へ」(top/bottom)，再试「注文確定」（部分页面仅有此按钮）"""
        driver = self.browser_manager.get_driver()
        primary_sel = self.order_confirm_page_config.get(
            "submit_to_payment_button", "input#edit-submit-confirm-top"
        )
        fallback_sel = self.order_confirm_page_config.get(
            "submit_to_payment_button_fallback", "input#edit-submit-confirm-bottom"
        )
        confirm_sel = self.order_confirm_page_config.get(
            "submit_confirm_order_button", "input[value='注文確定']"
        )
        clicked = False
        for sel, label in [
            (primary_sel, "決済情報の入力へ-上"),
            (fallback_sel, "決済情報の入力へ-下"),
            (confirm_sel, "注文確定"),
        ]:
            if not sel:
                continue
            try:
                self.logger.info("[支付流程] 点击进入下一步 (%s): %s", label, sel)
                btn = self.browser_manager.wait_for_clickable(By.CSS_SELECTOR, sel, timeout=8)
                driver.execute_script("arguments[0].click();", btn)
                clicked = True
                break
            except TimeoutException:
                self.logger.warning("[支付流程] %s 未找到或不可点击，尝试下一项", label)
            except Exception as e:
                self.logger.warning("[支付流程] 点击 %s 失败: %s，尝试下一项", label, e)
        if not clicked:
            raise RuntimeError("決済情報の入力へ / 注文確定 按钮均未找到或不可点击")
        time.sleep(self.order_confirm_page_config.get('wait_after_load_seconds', 3))
    
    def get_qr_code_image(self) -> Optional[bytes]:
        """
        获取支付二维码图片
        
        Returns:
            二维码图片的字节数据，如果获取失败返回None
        """
        driver = self.browser_manager.get_driver()
        qr_code_selector = self.cart_config.get('qr_code_selector', '')
        
        if not qr_code_selector:
            self.logger.warning("二维码选择器未配置")
            return None
        
        try:
            # 等待二维码生成
            wait_seconds = self.payment_config.get('qr_code_wait_seconds', 5)
            time.sleep(wait_seconds)
            
            # TODO: 根据实际页面结构实现二维码截图
            # 伪代码示例:
            # qr_element = driver.find_element(By.CSS_SELECTOR, qr_code_selector)
            # qr_screenshot = qr_element.screenshot_as_png
            # return qr_screenshot
            
            self.logger.warning("二维码获取功能需要根据实际页面实现")
            return None
        
        except Exception as e:
            self.logger.error(f"获取二维码失败: {e}")
            return None
    
    def check_order_completed(self) -> bool:
        """
        检查订单是否完成
        
        Returns:
            True表示订单已完成，False表示未完成
        """
        driver = self.browser_manager.get_driver()
        
        # TODO: 根据实际页面结构实现订单状态检查
        # 伪代码示例:
        # status_selector = self.cart_config.get('order_status_selector', '')
        # if status_selector:
        #     try:
        #         status_element = driver.find_element(By.CSS_SELECTOR, status_selector)
        #         status_text = status_element.text
        #         # 根据状态文本判断是否完成
        #         completed_keywords = ['完成', '成功', '已支付', 'completed', 'success']
        #         return any(keyword in status_text.lower() for keyword in completed_keywords)
        #     except:
        #         pass
        # 
        # return False
        
        self.logger.warning("订单状态检查功能需要根据实际页面实现")
        return False
    
    def _make_payment_summary(self, order: Dict[str, Any], payment_method: str = "") -> Dict[str, Any]:
        """构造 is_success.log 用的本单汇总结构（支付流程内使用）。"""
        return {
            "order_no": str(order.get("order_no") or order.get("order_id") or ""),
            "order_id": str(order.get("order_id") or ""),
            "success": False,
            "payment_method": payment_method,
            "failure_reason": "",
            "check_cart_requested": False,
            "check_cart_response": "未请求",
            "add_no_requested": False,
            "add_no_response": "未请求",
            "update_errors": [],
            "purchase_nos": [],
            "amount_total": None,
        }

    def process_payment(self, order: Dict[str, Any]) -> tuple:
        """
        执行支付流程：进入订单页 → 选择支付方式 → 点击「ご注文内容の確認へ」→ 后续二维码/状态检查
        
        Args:
            order: 订单字典（含 order_id、order_no、products 及商品 is_third_party 等标记）
            
        Returns:
            (success: bool, summary: dict) 供 is_success.log 汇总使用
        """
        order_id = order.get('order_id', '')
        self.logger.info(f"开始执行支付流程，订单: {order_id}")
        summary = self._make_payment_summary(order, "")
        self._last_confirm_amounts = {}
        
        try:
            # 1. 点击购物车「注文画面に進む」，进入订单确认页 cargo/order1
            self.click_pay_button()
            self.logger.info("[支付流程] 已进入订单确认页，等待页面稳定")
            time.sleep(self.order_page_config.get('wait_after_load_seconds', 3))
            if self.session_guard:
                try:
                    self.session_guard.ensure_logged_in(resume_url=None)
                    if self.session_guard.is_login_page(self.browser_manager.get_driver()):
                        raise SurugayaLoginError("支付页仍为登录状态")
                except SurugayaLoginError as login_err:
                    self.logger.error("[支付流程] 登录恢复失败: %s", login_err)
                    try:
                        self.feishu_notifier.notify_order_issue(
                            order_id,
                            [str(login_err)],
                            user_id=order.get("user_id"),
                            extra="骏河屋支付前掉登录，自动登录失败，请人工登录后重试。",
                        )
                    except Exception as feishu_err:
                        self.logger.warning("[支付流程] 飞书提醒发送失败: %s", feishu_err)
                    summary["failure_reason"] = "骏河屋登录失败: %s" % login_err
                    return False, summary
            
            # 2. 选择支付方式（页面优先级：0円代引 > PayPal > PayPay）
            self.logger.info("[支付流程] 开始选择支付方式")
            payment_method = self.select_payment_method(order)  # cash / paypay / paypal
            summary["payment_method"] = payment_method
            self.logger.info("[支付流程] 支付方式已选择: %s", payment_method)
            
            # 2.1 PayPay 专用：扫码时间段校验 + 提前飞书通知并等待
            if payment_method == "paypay":
                if not self._is_in_paypay_scan_window():
                    self.logger.warning("[支付流程] 当前不在 PayPay 扫码时间段内，跳过本单（在选择支付方式的骏河屋页面结束）")
                    try:
                        self.feishu_notifier.notify_order_issue(
                            order_id,
                            ["当前不在 PayPay 扫码时间段内，已跳过本单"],
                            user_id=order.get("user_id"),
                            extra="请稍后在配置的扫码时间段内重试，或人工处理。",
                        )
                    except Exception as feishu_err:
                        self.logger.warning("[支付流程] 飞书提醒发送失败: %s", feishu_err)
                    summary["failure_reason"] = "当前不在 PayPay 扫码时间段内，已跳过本单"
                    return False, summary
                wait_sec = self.paypay_advance_notify_wait_seconds
                self.logger.info("[支付流程] PayPay：先发飞书提醒，等待 %s 秒后再进入扫码页面", wait_sec)
                try:
                    # 文字准备通知走预警群；扫码群仅发二维码截图
                    self.feishu_notifier.send_message(
                        "PayPay 扫码准备",
                        f"**订单ID**: {order_id}\n\n即将进入 PayPay 扫码页面，**{wait_sec} 秒后**将发送二维码，请工作人员准备。",
                        use_paypay_scan_webhook=False,
                    )
                except Exception as feishu_err:
                    self.logger.warning("[支付流程] PayPay 提前通知飞书发送失败（继续流程）: %s", feishu_err)
                time.sleep(wait_sec)
            
            # 3. 点击「ご注文内容の確認へ」进入订单确认页（cargo/orderconfirm）
            self.logger.info("[支付流程] 开始点击确认按钮")
            self.click_confirm_order_button()
            self.logger.info("[支付流程] 已进入订单确认页，开始结算金额校验（截图+checkCartGoodsSimple）")

            # 4. 结算页校验：解析合計金額、全页截图上传、checkCartGoodsSimple；若 Success=false 或 Data=false 则停止并飞书
            ok, err, check_cart_raw = self.verify_confirm_page_and_check_cart(order)
            summary["check_cart_requested"] = True
            summary["check_cart_response"] = check_cart_raw if check_cart_raw else "(无响应体)"
            if not ok:
                self.logger.error("[支付流程] 结算校验未通过，停止后续操作: %s", err)
                try:
                    self.feishu_notifier.notify_order_issue(
                        order_id,
                        [err or "checkCartGoodsSimple 返回 Success=false 或 Data=false"],
                        user_id=order.get("user_id"),
                        extra="结算校验未通过，已停止后续操作，请人工跟进。",
                    )
                except Exception as feishu_err:
                    self.logger.warning("[支付流程] 飞书提醒发送失败: %s", feishu_err)
                summary["failure_reason"] = err or "checkCartGoodsSimple 返回 Success=false 或 Data=false"
                return False, summary

            # 5. 校验通过后点击「決済情報の入力へ」
            self.click_submit_to_payment_button()

            # 6. 支付后续流程（按支付方式分支）
            if payment_method == "paypay":
                # 进入扫码页面：截图上传并发飞书（二维码 5 分钟有效；刷新页面不会更新二维码，仅会锁定购物车）
                self._send_page_screenshot_to_feishu(
                    order_id,
                    "PayPay 扫码支付二维码（5分钟有效）",
                    extra_hint="请扫码完成支付。5分钟内未完成则购物车锁定，系统将发飞书并等待5分钟后处理下一单。",
                    use_paypay_scan_webhook=True,
                )
            elif payment_method == "paypal":
                # 进入 PayPal 中间页：点击继续确认按钮
                self._handle_paypal_middle_page()
            else:
                # 代金引換：通常会直接进入成功页，继续走统一轮询即可
                pass

            # 7. 每 30 秒检测一次是否进入成功页（不刷新页面，避免已到注文完了被刷掉）
            if payment_method == "paypay":
                timeout_seconds = 300  # PayPay 仅等 5 分钟，超时后发飞书并等 5 分钟再跳过
            else:
                timeout_seconds = 1800
            completed = self._poll_until_completed(
                order_id, timeout_seconds=timeout_seconds, interval_seconds=30, payment_method=payment_method
            )
            if not completed:
                summary["failure_reason"] = "支付后等待超时或未进入注文完了页"
                return False, summary

            # 8. 成功页：截图上传并飞书通知（用于留痕）
            self._send_page_screenshot_to_feishu(order_id, "下单成功页面截图（ご注文完了）")

            amounts = getattr(self, "_last_confirm_amounts", None) or {}
            summary["amount_total"] = amounts.get("total")

            # 9. 提取全部取引番号（可能多分单）
            purchase_nos = self._extract_all_purchase_nos()
            summary["purchase_nos"] = list(purchase_nos or [])
            if not purchase_nos:
                try:
                    self.feishu_notifier.notify_order_issue(
                        order_id,
                        ["已进入成功页，但未能提取取引番号（torihiki_num）"],
                        user_id=order.get("user_id"),
                        extra="请人工确认并补录商家订单号。",
                    )
                except Exception as e:
                    self.logger.warning("[支付流程] 飞书提醒发送失败: %s", e)
                # PayPay：即使未解析到取引番号也写对账底（金额/RS/时间），便于财务核对
                if payment_method == "paypay":
                    try:
                        from src.utils.paypay_scan_ledger import append_paypay_scan_record

                        path = append_paypay_scan_record(
                            self.config,
                            order=order,
                            purchase_nos=[],
                            amount_total=amounts.get("total"),
                            amount_goods=amounts.get("goods_fee"),
                            amount_operate=amounts.get("operate_fee"),
                            payment_method="paypay",
                            add_no_ok=None,
                            note="成功页未解析到取引番号，需人工补录",
                        )
                        self.logger.info("[支付流程] PayPay 对账留底已写入: %s", path)
                    except Exception as ledger_err:
                        self.logger.warning(
                            "[支付流程] PayPay 对账留底写入失败: %s", ledger_err
                        )
                summary["failure_reason"] = "已进入成功页但未能提取取引番号（torihiki_num）"
                summary["success"] = True
                return True, summary

            store_name = "骏河屋"
            products = order.get("products") or []
            if products and (products[0].get("shop_id") or "").strip():
                store_name = (products[0].get("shop_id") or "").strip()
            use_curl = (self.config.get("order_api") or {}).get("use_curl_for_order_api", True)
            
            # 10. 先构造 addNoCallbackSimple 需要的分单列表（no + 详情页 url）
            purchase_nos_for_add = []
            for purchase_no in purchase_nos:
                detail_url = self._build_purchase_url(purchase_no) or ""
                purchase_nos_for_add.append({"no": purchase_no, "url": detail_url})
            
            # 11. addNoCallbackSimple 优先：只要该接口 Success=true 且 Data=true，即判定支付流程成功
            credit_card = "货到付款"
            if payment_method == "paypay":
                credit_card = "paypay"
            elif payment_method == "paypal":
                credit_card = "paypal2167"
            ok, err, add_no_raw = send_add_no_callback(order, purchase_nos_for_add, credit_card, self.config, use_curl=use_curl)
            summary["add_no_requested"] = True
            summary["add_no_response"] = add_no_raw if add_no_raw else "(无响应体)"

            # PayPay 扫码成功：本地对账留底（RS / 时间 / 金额 / 取引番号）
            if payment_method == "paypay":
                try:
                    from src.utils.paypay_scan_ledger import append_paypay_scan_record

                    path = append_paypay_scan_record(
                        self.config,
                        order=order,
                        purchase_nos=list(purchase_nos),
                        amount_total=amounts.get("total"),
                        amount_goods=amounts.get("goods_fee"),
                        amount_operate=amounts.get("operate_fee"),
                        payment_method="paypay",
                        add_no_ok=bool(ok),
                        note="" if ok else ("addNoCallbackSimple 失败: %s" % (err or "")),
                    )
                    self.logger.info("[支付流程] PayPay 对账留底已写入: %s", path)
                except Exception as ledger_err:
                    self.logger.warning(
                        "[支付流程] PayPay 对账留底写入失败: %s", ledger_err
                    )

            if not ok:
                try:
                    self.feishu_notifier.notify_order_issue(
                        order_id,
                        [f"addNoCallbackSimple 回调失败: {err}"],
                        user_id=order.get("user_id"),
                        extra="网站已完成下单，但回调失败，已停止后续操作，请人工跟进。",
                    )
                except Exception as e:
                    self.logger.warning("[支付流程] 飞书提醒发送失败(回调失败): %s", e)
                summary["failure_reason"] = err or "addNoCallbackSimple 返回 Success=false 或 Data=false"
                return False, summary
            
            # 12. addNoCallbackSimple 成功后，再执行每个分单的 updateGoodsNoCallback（失败仅飞书提醒，不再影响整体支付结果）
            update_errors = []
            for purchase_no in purchase_nos:
                detail_url = self._build_purchase_url(purchase_no)
                if not detail_url:
                    update_errors.append(f"取引番号 {purchase_no} 未配置详情 URL")
                    continue
                try:
                    driver = self.browser_manager.get_driver()
                    driver.get(detail_url)
                    time.sleep(self.order_confirm_page_config.get("wait_after_load_seconds", 3))
                    goods_list = self._parse_detail_table_goods(driver)
                    path = take_full_page_screenshot(driver)
                    screenshot_url = upload_screenshot_get_url(path, self.config, use_curl=use_curl)
                    ok, err = send_update_goods_no_callback(
                        order, purchase_no, goods_list, screenshot_url or "", store_name,
                        self.config, use_curl=use_curl,
                    )
                    if not ok:
                        update_errors.append(f"取引番号 {purchase_no} updateGoodsNoCallback 失败: {err}")
                except Exception as e:
                    self.logger.warning("[支付流程] 分单 %s 处理异常: %s", purchase_no, e)
                    update_errors.append(f"取引番号 {purchase_no} 处理异常: {e}")
            
            if update_errors:
                try:
                    self.feishu_notifier.notify_order_issue(
                        order_id,
                        update_errors,
                        user_id=order.get("user_id"),
                        extra="网站已完成下单，分单回调有失败，请尽快排查。",
                    )
                except Exception as e:
                    self.logger.warning("[支付流程] 飞书提醒发送失败: %s", e)
            
            summary["success"] = True
            summary["update_errors"] = update_errors
            return True, summary
        
        except Exception as e:
            self.logger.error(f"支付流程执行失败: {e}", exc_info=True)
            summary["failure_reason"] = "支付流程异常: %s" % e
            return False, summary

