"""
页面处理器
"""

from selenium.webdriver.common.by import By
from typing import Dict, Any, Optional, Tuple
import time
import logging

from src.utils.logger import LoggerMixin
from src.utils.retry import retry
from src.auth.surugaya_session import SurugayaLoginError, SurugayaSessionGuard
from src.auth.cloudflare_guard import CloudflareChallengeError, CloudflareGuard


class PageHandler(LoggerMixin):
    """页面处理器"""
    
    def __init__(self, browser_manager, config: Dict[str, Any]):
        """
        初始化页面处理器
        
        Args:
            browser_manager: 浏览器管理器实例
            config: 配置字典
        """
        self.browser_manager = browser_manager
        self.config = config
        self.product_config = config.get('product_page', {})
        self.session_guard: Optional[SurugayaSessionGuard] = None
        self.cf_guard: Optional[CloudflareGuard] = None
        self._cart_verifier = None
        if SurugayaSessionGuard.is_enabled(config):
            self.session_guard = SurugayaSessionGuard(browser_manager, config)
            self.cf_guard = CloudflareGuard(browser_manager, config)

    def bind_cart_verifier(self, cart_verifier) -> None:
        """供加购中途掉登录后，从购物车回读已加数量。"""
        self._cart_verifier = cart_verifier

    def _ensure_session(self, resume_url: Optional[str] = None) -> None:
        if self.cf_guard:
            try:
                self.cf_guard.ensure_passed(
                    resume_url=resume_url, context="骏河屋页面导航"
                )
            except CloudflareChallengeError as e:
                raise SurugayaLoginError(str(e)) from e
        if not self.session_guard:
            return
        self.session_guard.ensure_logged_in(resume_url=resume_url)

    def _cart_qty_for_product(self, product_url: str) -> Optional[int]:
        """登录恢复后从购物车统计该商品数量；失败返回 None。"""
        if not self._cart_verifier:
            return None
        try:
            self._cart_verifier.open_cart_page()
            items = self._cart_verifier.fetch_cart_data() or []
        except Exception as e:
            self.logger.warning("回读购物车数量失败: %s", e)
            return None
        target = SurugayaSessionGuard.product_id_from_url(product_url)
        if not target:
            return None
        total = 0
        for item in items:
            pid = SurugayaSessionGuard.product_id_from_url(str(item.get("url") or ""))
            if pid == target or (pid and target and (pid.startswith(target) or target.startswith(pid))):
                try:
                    total += int(item.get("quantity") or 1)
                except Exception:
                    total += 1
        self.logger.info("购物车回读商品 %s 数量=%s", target, total)
        return total
    
    def open_product_page(self, product_url: str, max_retry: int = 3):
        """
        打开商品页面，失败时自动重试刷新（最多 max_retry 次）。

        注意：若已进入 Cloudflare 人机（含 URL 带 __cf_chl_rt_tk），不要反复 driver.get，
        否则会加深挑战死循环；应停在当前窗口等人机通过。
        """
        driver = self.browser_manager.get_driver()
        attempt = 0
        last_error: Exception | None = None
        
        while attempt < max_retry:
            attempt += 1
            try:
                if attempt == 1:
                    self.logger.info(f"打开商品页面: {product_url}")
                else:
                    self.logger.info(f"重新加载商品页面（第 {attempt} 次）: {product_url}")

                # 已在挑战中：禁止再次 get，交给门卫等待人工
                if self.cf_guard and self.cf_guard.is_challenge_page(driver):
                    self.logger.warning(
                        "打开商品页前已处于 Cloudflare 挑战，跳过重复导航，等待人工通过"
                    )
                    self._ensure_session(resume_url=product_url)
                    return

                from src.browser.browser_manager import BrowserManager

                BrowserManager.navigate_allow_timeout(driver, product_url, self.logger)

                # 短轮询等业务内容，避免固定长 sleep；遇人机则交给门卫
                wait_seconds = float(self.product_config.get('wait_after_load_seconds', 2) or 2)
                deadline = time.time() + max(1.0, min(wait_seconds, 4.0))
                while time.time() < deadline:
                    if self.cf_guard and self.cf_guard.is_challenge_page(driver):
                        break
                    try:
                        src = driver.page_source or ""
                        title = (driver.title or "")
                    except Exception:
                        src, title = "", ""
                    if "商品" in title or "suruga-ya" in src.lower() or "quantity_selection" in src:
                        break
                    time.sleep(0.3)

                self._ensure_session(resume_url=product_url)
                
                self.logger.debug("商品页面加载完成")
                return
            except SurugayaLoginError:
                # 人机超时等：不要重试 get，直接抛给上层
                raise
            except Exception as e:
                last_error = e
                self.logger.warning(f"打开商品页面失败（第 {attempt} 次）: {e}")
                # 若失败后已掉进人机页，停止重试，转等待
                if self.cf_guard and self.cf_guard.is_challenge_page(driver):
                    self.logger.warning("打开失败后已进入人机验证，停止重试并等待人工")
                    self._ensure_session(resume_url=product_url)
                    return
                if attempt < max_retry:
                    time.sleep(1.0)
        
        # 超过最大重试次数仍失败，抛出最后一次错误
        raise RuntimeError(f"多次尝试打开商品页面失败（共 {max_retry} 次）: {last_error}")
    
    def is_r18_product(self) -> bool:
        """
        判断当前商品页面是否为 R18 商品。
        
        依据：
        - R18 警告页包含「この先のページは成年向け商品を含んでおり現在のセーフサーチ設定では表示できません。」
        - 商品详情页包含「この商品は成人向け商品です。18歳以上の方のみご購入できます。」
        """
        driver = self.browser_manager.get_driver()
        try:
            html = driver.page_source or ""
        except Exception as e:
            self.logger.warning(f"获取页面源码失败，无法判断是否为R18商品: {e}")
            return False
        
        keywords = [
            "この商品は成人向け商品です。18歳以上の方のみご購入できます。",
            "この先のページは成年向け商品を含んでおり現在のセーフサーチ設定では表示できません。",
        ]
        for kw in keywords:
            if kw in html:
                self.logger.info("检测到 R18 商品页面")
                return True
        return False

    def is_presale_product(self) -> bool:
        """
        判断当前商品页是否为预售（尚未发售、仅可预约）商品。

        依据：详情页 product_note 内常见提示（span.red_s）：
        「この商品はまだ発売されておりません。予約すると発売日以降に出荷できます。」
        """
        driver = self.browser_manager.get_driver()
        try:
            html = driver.page_source or ""
        except Exception as e:
            self.logger.warning("获取页面源码失败，无法判断是否为预售商品: %s", e)
            return False

        keywords = [
            "この商品はまだ発売されておりません。予約すると発売日以降に出荷できます。",
        ]
        for kw in keywords:
            if kw in html:
                self.logger.info("检测到预售（未発売）商品页面")
                return True
        return False

    @retry(max_attempts=3, delay=2.0)
    def check_stock(self, product_url: str, required_quantity: int) -> Tuple[bool, str]:
        """
        检查商品库存
        
        Args:
            product_url: 商品URL
            required_quantity: 需要的数量
            
        Returns:
            (是否充足, 消息)
        """
        driver = self.browser_manager.get_driver()
        stock_selector = self.product_config.get('stock_check_selector', '')
        
        if not stock_selector:
            self.logger.warning("库存检查选择器未配置，跳过库存检查")
            return True, ""
        
        try:
            # 等待库存元素出现
            stock_element = self.browser_manager.wait_for_element(
                By.CSS_SELECTOR, stock_selector, timeout=10
            )
            
            # 获取value属性（库存数量）
            stock_value = stock_element.get_attribute('value')
            if not stock_value:
                self.logger.warning(f"无法获取库存值，元素: {stock_selector}")
                return True, ""  # 无法获取时，假设库存充足
            
            current_stock = int(stock_value)
            self.logger.info(f"当前库存: {current_stock}，需要数量: {required_quantity}")
            
            if current_stock < required_quantity:
                message = f"商品库存不足，您下单数量为{required_quantity}，目前为{current_stock}"
                self.logger.warning(message)
                return False, message
            
            self.logger.info(f"库存充足: {current_stock} >= {required_quantity}")
            return True, ""
        
        except Exception as e:
            self.logger.error(f"检查库存时出错: {e}")
            # 库存检查失败，为了安全起见，返回False
            return False, f"库存检查失败: {e}"
    
    @retry(max_attempts=3, delay=2.0)
    def check_price(self, product_url: str, order_price: float) -> Tuple[bool, str]:
        """
        检查商品单价
        
        Args:
            product_url: 商品URL
            order_price: 订单中的单价
            
        Returns:
            (价格是否可接受, 消息)
        """
        driver = self.browser_manager.get_driver()
        price_selector = self.product_config.get('price_check_selector', '')
        
        if not price_selector:
            self.logger.warning("价格检查选择器未配置，跳过价格检查")
            return True, ""
        
        try:
            # 等待价格元素出现
            price_element = self.browser_manager.wait_for_element(
                By.CSS_SELECTOR, price_selector, timeout=10
            )
            
            # 获取价格文本（例如："9,805円 (税込)"）
            price_text = price_element.text.strip()
            if not price_text:
                self.logger.warning(f"无法获取价格文本，元素: {price_selector}")
                return True, ""  # 无法获取时，假设价格可接受
            
            # 解析价格
            current_price = self._parse_price(price_text)
            self.logger.info(f"当前价格: {current_price}円，订单价格: {order_price}円")
            
            if current_price > order_price:
                message = f"商品价格有变动，您下单时为{order_price}円，目前为{current_price}円"
                self.logger.warning(message)
                return False, message
            
            self.logger.info(f"价格可接受: {current_price}円 <= {order_price}円")
            return True, ""
        
        except Exception as e:
            self.logger.error(f"检查价格时出错: {e}")
            # 价格检查失败，为了安全起见，返回False
            return False, f"价格检查失败: {e}"
    
    def select_quantity(self, quantity: int) -> int:
        """
        选择商品数量并读回实际选中值。

        - qty<=1：不操作，返回 1
        - 可选最大值 < 目标：先设为可选最大值并返回该值（由 add_to_cart 补加）
        - 找不到控件或读回失败：抛错（不再静默当作 1）
        """
        qty = max(1, int(quantity or 1))
        if qty <= 1:
            return 1

        driver = self.browser_manager.get_driver()
        quantity_selector = (
            self.product_config.get("quantity_selector") or "select#quantity_selection"
        ).strip()
        from selenium.webdriver.support.ui import Select

        quantity_element = self.browser_manager.wait_for_element(
            By.CSS_SELECTOR, quantity_selector, timeout=5
        )
        select = Select(quantity_element)
        available_options: list = []
        for opt in select.options:
            raw = (opt.get_attribute("value") or "").strip()
            if raw.isdigit():
                available_options.append(int(raw))
        if not available_options:
            raise RuntimeError("数量下拉无可用数字选项")

        max_quantity = max(available_options)
        selected_quantity = min(qty, max_quantity)
        if selected_quantity < qty:
            self.logger.info(
                "数量下拉最大可选 %s，目标 %s，本页先设为 %s（将补加）",
                max_quantity,
                qty,
                selected_quantity,
            )
        else:
            self.logger.info(
                "选择商品数量: %s（订单需要: %s，最大可选: %s）",
                selected_quantity,
                qty,
                max_quantity,
            )

        select.select_by_value(str(selected_quantity))
        time.sleep(0.35)
        try:
            actual = int(
                (select.first_selected_option.get_attribute("value") or "").strip() or "0"
            )
        except Exception:
            actual = 0
        if actual != selected_quantity:
            raise RuntimeError(
                "数量设置后读回不一致: 期望=%s 实际=%s" % (selected_quantity, actual)
            )
        return actual

    def add_to_cart(self, product_url: str, quantity: int = 1) -> int:
        """
        加入购物车。返回实际加入的总数量。
        若单次下拉无法达到目标数量，会重新打开详情页补加，直到凑齐或失败。
        注意：不做整函数重试，避免补加中途失败后重复加购导致超量。
        """
        driver = self.browser_manager.get_driver()
        add_cart_selector = self.product_config.get("add_to_cart_selector", "")

        if not add_cart_selector:
            self.logger.warning("加入购物车按钮选择器未配置，跳过加入购物车操作")
            self.logger.info("请配置 product_page.add_to_cart_selector 后重试")
            raise ValueError(
                "加入购物车按钮选择器未配置，请在config.yaml中配置product_page.add_to_cart_selector"
            )

        target = max(1, int(quantity or 1))
        added_total = 0
        max_rounds = max(target + 2, 3)

        try:
            self._ensure_session(resume_url=product_url)
            for round_idx in range(max_rounds):
                remaining = target - added_total
                if remaining <= 0:
                    break

                if round_idx > 0:
                    self.logger.info(
                        "补加购：已加 %s/%s，重新打开详情页继续",
                        added_total,
                        target,
                    )
                    self.open_product_page(product_url)

                try:
                    batch_qty = self.select_quantity(remaining)
                except Exception as e:
                    if remaining <= 1 and added_total == 0:
                        self.logger.warning("选择数量失败，按默认 1 件加购: %s", e)
                        batch_qty = 1
                    else:
                        raise RuntimeError(
                            "选择数量失败（目标剩余=%s）: %s" % (remaining, e)
                        ) from e

                add_button = self.browser_manager.wait_for_clickable(
                    By.CSS_SELECTOR, add_cart_selector, timeout=10
                )
                self.logger.info(
                    "点击加入购物车（本批数量=%s，累计目标=%s）", batch_qty, target
                )
                driver.execute_script("arguments[0].click();", add_button)

                wait_seconds = self.product_config.get("wait_after_load_seconds", 2)
                time.sleep(wait_seconds)

                # 加购后若被踢到登录页：登录 → 以购物车实数为准补齐剩余
                if self.session_guard and self.session_guard.is_login_page(driver):
                    self.logger.warning("加购后出现登录页，尝试自动登录并回读购物车数量")
                    self.session_guard.ensure_logged_in(resume_url=None)
                    cart_qty = self._cart_qty_for_product(product_url)
                    if cart_qty is not None:
                        added_total = min(target, max(0, int(cart_qty)))
                        self.logger.info(
                            "登录恢复后以购物车为准：已加=%s/%s", added_total, target
                        )
                    self.open_product_page(product_url)
                    continue

                added_total += batch_qty
                if batch_qty >= remaining:
                    break
                if batch_qty < 1:
                    break

            if added_total < target:
                raise RuntimeError(
                    "加购数量不足: 目标=%s, 实际=%s" % (target, added_total)
                )

            self.logger.info("商品已加入购物车（实际数量: %s）", added_total)
            return added_total
        except SurugayaLoginError:
            raise
        except Exception as e:
            self.logger.error("加入购物车失败: %s", e)
            raise

    def _parse_stock(self, stock_text: str) -> int:
        """
        解析库存文本
        
        Args:
            stock_text: 库存文本
            
        Returns:
            库存数量
        """
        # TODO: 根据实际页面格式实现
        # 例如: "库存: 10" -> 10
        # 或者: "在庫あり(10)" -> 10
        try:
            import re
            numbers = re.findall(r'\d+', stock_text)
            if numbers:
                return int(numbers[0])
        except:
            pass
        return 0
    
    def _parse_price(self, price_text: str) -> float:
        """
        解析价格文本
        
        Args:
            price_text: 价格文本，例如："9,805円 (税込)" 或 "¥1,000"
            
        Returns:
            价格数值（浮点数）
        """
        try:
            import re
            # 移除所有非数字字符，但保留逗号和点
            # 先移除逗号，然后提取数字
            cleaned = price_text.replace(',', '').replace('，', '')  # 移除逗号
            
            # 提取数字（包括小数点）
            numbers = re.findall(r'\d+\.?\d*', cleaned)
            if numbers:
                return float(numbers[0])
            
            # 如果没有找到数字，尝试提取所有数字字符
            digits = re.sub(r'[^\d.]', '', cleaned)
            if digits:
                return float(digits)
        except Exception as e:
            self.logger.warning(f"解析价格失败: {price_text}, 错误: {e}")
        return 0.0

