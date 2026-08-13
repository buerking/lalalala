"""
购物车验证器
"""

from selenium.webdriver.common.by import By
from typing import Dict, Any, List, Tuple
import time
import logging
import re

from src.utils.logger import LoggerMixin
from src.utils.retry import retry
from src.auth.surugaya_session import SurugayaSessionGuard
from src.auth.cloudflare_guard import CloudflareChallengeError, CloudflareGuard


class CartVerifier(LoggerMixin):
    """购物车验证器"""
    
    def __init__(self, browser_manager, config: Dict[str, Any]):
        """
        初始化购物车验证器
        
        Args:
            browser_manager: 浏览器管理器实例
            config: 配置字典
        """
        self.browser_manager = browser_manager
        self.config = config
        self.cart_config = config.get('cart_verification', {})
        self.page_config = config.get('cart_page', {})
        self.session_guard = None
        self.cf_guard = None
        if SurugayaSessionGuard.is_enabled(config):
            self.session_guard = SurugayaSessionGuard(browser_manager, config)
            self.cf_guard = CloudflareGuard(browser_manager, config)
    
    def open_cart_page(self):
        """打开购物车页面（超时不卡死；空车快速放行）。"""
        from src.browser.browser_manager import BrowserManager

        driver = self.browser_manager.get_driver()
        cart_url = self.page_config.get('url', '')
        
        if not cart_url:
            raise ValueError("购物车页面URL未配置")
        
        self.logger.info(f"打开购物车页面: {cart_url}")
        BrowserManager.navigate_allow_timeout(driver, cart_url, self.logger)

        # 短轮询：空车文案或业务结构出现则尽快继续，避免固定 sleep + CF 误等拖到数分钟
        wait_seconds = float(self.page_config.get('wait_after_load_seconds', 3) or 3)
        deadline = time.time() + max(1.5, min(wait_seconds, 5.0))
        empty_markers = (
            "現在購入予定アイテムはありません",
            "購入予定アイテムはありません",
        )
        while time.time() < deadline:
            try:
                src = driver.page_source or ""
            except Exception:
                src = ""
            if any(m in src for m in empty_markers):
                self.logger.info("购物车页已出现空车文案，跳过额外等待")
                break
            if self.cf_guard and self.cf_guard.is_challenge_page(driver):
                break
            time.sleep(0.35)
        else:
            time.sleep(0.2)

        if self.cf_guard:
            try:
                self.cf_guard.ensure_passed(resume_url=cart_url, context="打开购物车")
            except CloudflareChallengeError as e:
                raise RuntimeError(str(e)) from e
        if self.session_guard:
            self.session_guard.ensure_logged_in(resume_url=cart_url)
    
    @retry(max_attempts=3, delay=2.0)
    def clear_cart(self):
        """
        清空购物车（带重试机制）
        
        重要：清空购物车失败会导致下一个订单数据污染，因此必须重试直到成功
        
        清空策略：
        1. 首先尝试使用"全削除"按钮一键清空
        2. 如果全删除失败或按钮不存在，则依次单个删除每个商品
        
        Returns:
            True表示清空成功，False表示失败（重试后仍失败）
        """
        driver = self.browser_manager.get_driver()
        clear_selector = self.page_config.get('clear_cart_selector', '')
        product_list_selector = self.page_config.get('product_list_selector', '')
        remove_product_selector = self.page_config.get('remove_product_selector', 'a.remove_product')
        
        try:
            # 打开购物车页面
            self.open_cart_page()
            
            # 检查购物车是否为空
            if product_list_selector:
                products = self.browser_manager.find_elements_now(
                    By.CSS_SELECTOR, product_list_selector
                )
                if len(products) == 0:
                    self.logger.info("购物车已为空，无需清空")
                    return True
            
            # ============================================================
            # 策略1：尝试使用"全削除"按钮一键清空
            # ============================================================
            if clear_selector:
                try:
                    self.logger.info("尝试使用全削除按钮清空购物车")
                    clear_button = self.browser_manager.wait_for_clickable(
                        By.CSS_SELECTOR, clear_selector, timeout=5
                    )
                    
                    # 使用JavaScript点击，更可靠
                    driver.execute_script("arguments[0].click();", clear_button)
                    
                    # 等待清空完成（可能需要确认对话框或页面刷新）
                    time.sleep(3)
                    
                    # 刷新页面，确保获取最新状态
                    driver.refresh()
                    time.sleep(2)
                    
                    # 检查是否清空成功
                    if product_list_selector:
                        products = self.browser_manager.find_elements_now(
                            By.CSS_SELECTOR, product_list_selector
                        )
                        if len(products) == 0:
                            self.logger.info("使用全削除按钮成功清空购物车")
                            return True
                        else:
                            self.logger.warning(
                                f"全削除按钮点击后仍有 {len(products)} 个商品，"
                                f"将尝试单个删除"
                            )
                except Exception as e:
                    self.logger.warning(f"全削除按钮不可用或失败: {e}，将尝试单个删除")
            else:
                self.logger.info("全削除按钮选择器未配置，直接使用单个删除方式")
            
            # ============================================================
            # 策略2：依次单个删除每个商品
            # ============================================================
            self.logger.info("开始逐个删除购物车中的商品")
            max_attempts = 10  # 最多尝试10次，避免无限循环
            attempt = 0
            
            while attempt < max_attempts:
                # 重新打开购物车页面，获取最新商品列表
                self.open_cart_page()
                time.sleep(1)
                
                if not product_list_selector:
                    self.logger.warning("商品列表选择器未配置，无法单个删除")
                    break
                
                # 获取当前购物车中的所有商品
                products = self.browser_manager.find_elements_now(
                    By.CSS_SELECTOR, product_list_selector
                )
                
                if len(products) == 0:
                    self.logger.info("购物车已清空（通过单个删除）")
                    return True
                
                self.logger.info(f"购物车中还有 {len(products)} 个商品，开始删除第一个商品")
                
                # 删除第一个商品
                try:
                    # 在第一个商品元素中查找删除按钮
                    first_product = products[0]
                    remove_button = first_product.find_element(By.CSS_SELECTOR, remove_product_selector)
                    
                    # 获取商品信息（用于日志）
                    try:
                        product_name_element = first_product.find_element(
                            By.CSS_SELECTOR, 
                            self.page_config.get('product_name_selector', '')
                        )
                        product_name = product_name_element.text.strip()[:50]
                    except:
                        product_name = "未知商品"
                    
                    self.logger.info(f"正在删除商品: {product_name}")
                    
                    # 使用JavaScript点击删除按钮
                    driver.execute_script("arguments[0].click();", remove_button)
                    
                    # 等待删除完成（页面可能会刷新或弹窗）
                    time.sleep(2)
                    
                    # 刷新页面，确保获取最新状态
                    driver.refresh()
                    time.sleep(1)
                    
                except Exception as e:
                    self.logger.error(f"删除商品时出错: {e}")
                    attempt += 1
                    continue
                
                attempt += 1
            
            # 最后检查购物车是否为空
            self.open_cart_page()
            time.sleep(1)
            products = self.browser_manager.find_elements_now(
                By.CSS_SELECTOR, product_list_selector
            )
            
            if len(products) == 0:
                self.logger.info("购物车已清空（通过单个删除）")
                return True
            else:
                raise Exception(
                    f"单个删除失败，尝试 {attempt} 次后仍有 {len(products)} 个商品"
                )
        
        except Exception as e:
            self.logger.error(f"清空购物车失败: {e}")
            raise  # 抛出异常以便重试机制捕获
    
    @retry(max_attempts=3, delay=2.0)
    def fetch_cart_data(self) -> List[Dict[str, Any]]:
        """
        抓取购物车数据
        
        重要说明：返回的是所有商品条目，包括重复的
        ============================================
        如果购物车中有商品A数量3，会返回3个独立的条目：
        [
            {'name': '商品A', 'url': '...', 'price': 1000, 'quantity': 1},
            {'name': '商品A', 'url': '...', 'price': 1000, 'quantity': 1},
            {'name': '商品A', 'url': '...', 'price': 1000, 'quantity': 1}
        ]
        
        这些重复条目需要在 verify_cart_data() 中按商品ID分组统计总数量。
        
        Returns:
            购物车商品列表，每个商品包含name、url、price、quantity
            注意：多数量商品会返回多个条目
        """
        driver = self.browser_manager.get_driver()
        base_url = self.page_config.get('base_url', 'https://www.suruga-ya.jp')
        
        products = []
        product_list_selector = self.page_config.get('product_list_selector', '')
        product_name_selector = self.page_config.get('product_name_selector', '')
        product_url_selector = self.page_config.get('product_url_selector', '')
        product_price_selector = self.page_config.get('product_price_selector', '')
        product_quantity_selector = self.page_config.get('product_quantity_selector', '')
        
        if not product_list_selector:
            self.logger.warning("商品列表选择器未配置")
            return products
        
        try:
            # 等待商品列表加载
            self.browser_manager.wait_for_element(
                By.CSS_SELECTOR, product_list_selector, timeout=10
            )
            
            # 获取所有商品行
            product_elements = self.browser_manager.find_elements_now(
                By.CSS_SELECTOR, product_list_selector
            )
            self.logger.info(f"找到 {len(product_elements)} 个商品")
            
            for idx, element in enumerate(product_elements, 1):
                try:
                    # 获取商品名称
                    name_element = element.find_element(By.CSS_SELECTOR, product_name_selector)
                    product_name = name_element.text.strip()
                    
                    # 获取商品URL（相对路径，需要拼接）
                    url_element = element.find_element(By.CSS_SELECTOR, product_url_selector)
                    relative_url = url_element.get_attribute('href')
                    if not relative_url:
                        # 如果没有href，尝试获取href属性
                        relative_url = url_element.get_attribute('href') or ''
                    
                    # 如果是相对路径，拼接基础URL
                    if relative_url.startswith('/'):
                        product_url = base_url + relative_url
                    elif relative_url.startswith('http'):
                        product_url = relative_url
                    else:
                        # 如果已经是完整URL，直接使用
                        product_url = relative_url if relative_url else ''
                    
                    # 获取价格
                    price_element = element.find_element(By.CSS_SELECTOR, product_price_selector)
                    price_text = price_element.text.strip()
                    product_price = self._parse_price(price_text)
                    
                    # 获取数量
                    quantity_element = element.find_element(By.CSS_SELECTOR, product_quantity_selector)
                    quantity_text = quantity_element.text.strip()
                    product_quantity = int(re.sub(r'[^\d]', '', quantity_text)) if quantity_text else 1
                    
                    product = {
                        'name': product_name,
                        'url': product_url,
                        'price': product_price,
                        'quantity': product_quantity
                    }
                    products.append(product)
                    
                    self.logger.debug(
                        f"商品 {idx}: {product_name}, "
                        f"价格: {product_price}円, 数量: {product_quantity}"
                    )
                
                except Exception as e:
                    self.logger.warning(f"抓取第 {idx} 个商品数据失败: {e}")
                    continue
            
            self.logger.info(f"成功抓取 {len(products)} 个商品数据")
            return products
        
        except Exception as e:
            self.logger.error(f"抓取购物车数据失败: {e}")
            return []
    
    def _parse_price(self, price_text: str) -> float:
        """
        解析价格文本
        
        Args:
            price_text: 价格文本，例如："7,600円" 或 "650円"
            
        Returns:
            价格数值（浮点数）
        """
        try:
            # 移除所有非数字字符，但保留逗号
            cleaned = price_text.replace(',', '').replace('，', '')
            
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
    
    def verify_cart_data(self, order: Dict[str, Any]) -> Tuple[bool, str]:
        """
        验证购物车数据与订单数据是否一致
        
        重要说明：骏河屋网站对多数量商品的处理方式
        ============================================
        订单格式：商品A，数量3（A,3）
        购物车格式：商品A、商品A、商品A（A,A,A）- 多个独立条目，每个条目数量通常为1
        
        因此不能直接比对购物车条目数量与订单商品数量，需要：
        1. 按商品ID分组统计购物车中每个商品的总数量
        2. 比对每个商品的总数量，而不是条目数量
        
        示例：
        - 订单：商品A数量3，商品B数量2
        - 购物车：商品A(数量1)、商品A(数量1)、商品A(数量1)、商品B(数量1)、商品B(数量1)
        - 统计后：商品A总数量3，商品B总数量2
        - 比对：订单A(3) = 购物车A(3) ✓，订单B(2) = 购物车B(2) ✓
        
        Args:
            order: 订单数据，包含products列表
                products格式: [
                    {'name': '商品A', 'url': '...', 'price': 1000, 'quantity': 3},
                    {'name': '商品B', 'url': '...', 'price': 2000, 'quantity': 2}
                ]
            
        Returns:
            (数据是否一致, 消息)
        """
        max_retry = self.cart_config.get('max_retry', 3)
        retry_interval = self.cart_config.get('retry_interval_seconds', 2)
        
        for attempt in range(max_retry):
            try:
                # 打开购物车页面
                self.open_cart_page()
                
                # 抓取购物车数据（返回所有商品条目，包括重复的）
                # 例如：商品A数量3会返回3个条目
                cart_products = self.fetch_cart_data()
                order_products = order.get('products', [])
                
                def extract_product_id(url: str) -> str:
                    """
                    从URL中提取商品ID
                    
                    重要说明：订单URL和购物车URL的格式可能不同
                    ============================================
                    订单URL格式：https://www.suruga-ya.jp/product/detail/BO2416546（基础ID）
                    购物车URL格式：/product/detail/BO2416546n（基础ID + 后缀）
                    
                    提取规则：直接提取URL最后一部分作为商品ID，不做任何处理
                    - https://www.suruga-ya.jp/product/detail/WK12916 -> WK12916
                    - /product/detail/WK12916 -> WK12916
                    - /product/detail/BO2416546n -> BO2416546n（保持原样）
                    - /product/detail/220301400001 -> 220301400001（保持原样）
                    """
                    if not url:
                        return ''
                    # 移除末尾的斜杠和查询参数
                    url = url.split('?')[0].rstrip('/')
                    # 提取最后一部分（商品ID）
                    parts = url.split('/')
                    product_id = parts[-1] if parts else ''
                    
                    # 直接返回商品ID（保持原样，不处理后缀）
                    # 匹配时会使用左匹配规则
                    return product_id.upper() if product_id else ''
                
                # ============================================================
                # 关键步骤1：按商品分组统计购物车中的商品
                # ============================================================
                # 因为多数量商品会显示为多个条目（A,A,A），需要分组统计
                # 格式：{product_id: {'count': 总数量, 'price': 价格, 'name': 名称, 'url': URL}}
                cart_product_groups = {}
                for cp in cart_products:
                    cart_url = cp.get('url', '')
                    cart_name = cp.get('name', '')
                    cart_price = cp.get('price', 0)
                    cart_quantity = cp.get('quantity', 1)  # 每个条目的数量（通常是1）
                    
                    cart_base_id = extract_product_id(cart_url)
                    
                    # 调试日志：输出提取的基础ID（仅在DEBUG级别）
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(
                            f"购物车商品条目: URL={cart_url}, 提取的基础ID={cart_base_id}"
                        )
                    
                    # 如果是新商品，初始化分组
                    if cart_base_id not in cart_product_groups:
                        cart_product_groups[cart_base_id] = {
                            'count': 0,
                            'price': cart_price,
                            'name': cart_name,
                            'url': cart_url
                        }
                    
                    # 累加数量（多个条目累加得到总数量）
                    # 例如：商品A出现3次，每次数量1，累加后总数量为3
                    cart_product_groups[cart_base_id]['count'] += cart_quantity
                
                # 计算购物车总商品条目数和总数量（用于日志）
                total_cart_items = sum(group['count'] for group in cart_product_groups.values())
                total_order_items = sum(p.get('quantity', 0) for p in order_products)
                
                self.logger.info(
                    f"购物车商品种类: {len(cart_product_groups)}，总数量: {total_cart_items}；"
                    f"订单商品种类: {len(order_products)}，总数量: {total_order_items}"
                )

                # ============================================================
                # 关键步骤1.5：按商品ID分组统计订单中的商品（合并重复行）
                # ============================================================
                # 订单接口可能返回重复的 goods（同一URL/同一GoodsNo 多行，每行 quantity=1）。
                # 若不先合并，会出现：
                # - 第一行：购物车数量=2/3/... vs 订单数量=1 -> 误报“数量不一致”
                # - 删除购物车分组后，后续重复行 -> 误报“未在购物车中找到”
                order_product_groups: Dict[str, Dict[str, Any]] = {}
                for op in order_products:
                    op_name = op.get('name', '')
                    op_url = op.get('url', '')
                    op_price = op.get('price', 0)
                    op_quantity = op.get('quantity', 0) or 0
                    op_id = extract_product_id(op_url)
                    if not op_id:
                        # URL 异常时保底：用 name 做 key（尽量不丢数据）
                        op_id = (op_name or '').strip().upper()
                    if op_id not in order_product_groups:
                        order_product_groups[op_id] = {
                            'id': op_id,
                            'name': op_name,
                            'url': op_url,
                            'price': op_price,
                            'quantity': 0,
                        }
                    order_product_groups[op_id]['quantity'] += int(op_quantity)
                    # 保留一个可读的 name/url（后续行若为空则补齐）
                    if not order_product_groups[op_id].get('name') and op_name:
                        order_product_groups[op_id]['name'] = op_name
                    if not order_product_groups[op_id].get('url') and op_url:
                        order_product_groups[op_id]['url'] = op_url

                self.logger.info(
                    f"订单商品按ID合并后：种类 {len(order_product_groups)}，总数量 {sum(v.get('quantity', 0) for v in order_product_groups.values())}"
                )
                
                # ============================================================
                # 关键步骤2：比对每个订单商品与购物车分组数据
                # ============================================================
                messages = []
                for order_product in order_product_groups.values():
                    order_name = order_product.get('name', '')
                    order_url = order_product.get('url', '')
                    order_price = order_product.get('price', 0)
                    order_quantity = order_product.get('quantity', 0)  # 订单中该商品的总数量（已合并）
                    order_product_id = order_product.get('id', '') or extract_product_id(order_url)
                    self.logger.debug(
                        f"订单商品: {order_name}, URL: {order_url}, 提取的商品ID: {order_product_id}"
                    )
                    
                    # ============================================================
                    # 关键匹配逻辑：使用左匹配规则
                    # ============================================================
                    # 规则：如果购物车商品ID以订单商品ID开头，则视为同一商品
                    # 示例：
                    # - 订单ID: BO2416546，购物车ID: BO2416546n → 匹配 ✓（购物车ID以订单ID开头）
                    # - 订单ID: 438011314，购物车ID: 438011314001 → 匹配 ✓（购物车ID以订单ID开头）
                    # - 订单ID: WK12916，购物车ID: WK12916 → 匹配 ✓（完全匹配）
                    
                    cart_group = None
                    matched_group_id = None
                    
                    # 方式1：精确匹配（完全相同的ID）
                    if order_product_id in cart_product_groups:
                        cart_group = cart_product_groups[order_product_id]
                        matched_group_id = order_product_id
                        self.logger.debug(f"  精确匹配成功: {order_product_id}")
                    else:
                        # 方式2：左匹配（购物车ID以订单ID开头）
                        self.logger.debug(f"精确匹配失败，尝试左匹配: 订单ID={order_product_id}")
                        for cp_id, cp_group in cart_product_groups.items():
                            # 如果购物车ID以订单ID开头，则匹配
                            if cp_id.startswith(order_product_id):
                                cart_group = cp_group
                                matched_group_id = cp_id
                                self.logger.debug(
                                    f"  左匹配成功: 购物车ID {cp_id} 以订单ID {order_product_id} 开头"
                                )
                                break
                        
                        # 方式3：商品名称匹配（备用方案）
                        if cart_group is None:
                            self.logger.debug(f"左匹配失败，尝试商品名称匹配")
                            for cp_id, cp_group in cart_product_groups.items():
                                cart_group_name = cp_group.get('name', '')
                                if order_name and cart_group_name:
                                    if order_name.strip() == cart_group_name.strip():
                                        cart_group = cp_group
                                        matched_group_id = cp_id
                                        self.logger.debug(f"  通过商品名称匹配成功")
                                        break
                    
                    if cart_group is None:
                        # 输出所有购物车商品的ID，便于调试
                        cart_ids = list(cart_product_groups.keys())
                        self.logger.warning(
                            f"商品 {order_name} (订单ID: {order_product_id}, URL: {order_url}) "
                            f"未在购物车中找到。购物车中的ID列表: {cart_ids}"
                        )
                        messages.append(f"商品 {order_name} (URL: {order_url}) 未在购物车中找到")
                        continue
                    
                    self.logger.debug(
                        f"商品 {order_name} 匹配成功: 订单ID={order_product_id}, "
                        f"购物车ID={matched_group_id}"
                    )
                    
                    # 比对价格和总数量
                    cart_price = cart_group.get('price', 0)
                    cart_total_quantity = cart_group.get('count', 0)  # 购物车中该商品的总数量（多个条目累加）
                    
                    # 价格比对：订单价格 vs 购物车价格
                    if abs(cart_price - order_price) > 0.01:  # 允许0.01的误差
                        messages.append(
                            f"商品 {order_name} 价格不一致，"
                            f"订单: {order_price}円，购物车: {cart_price}円"
                        )
                    
                    # 数量比对：订单数量 vs 购物车总数量（关键：比对总数量，不是条目数量）
                    # 例如：订单A数量3，购物车A总数量3（3个条目，每个数量1）-> 匹配 ✓
                    if cart_total_quantity != order_quantity:
                        messages.append(
                            f"商品 {order_name} 数量不一致，"
                            f"订单: {order_quantity}，购物车: {cart_total_quantity}"
                        )
                    
                    # 从分组中移除已匹配的商品（用于后续检查是否有多余商品）
                    if matched_group_id in cart_product_groups:
                        del cart_product_groups[matched_group_id]
                
                # ============================================================
                # 关键步骤3：检查购物车中是否有订单中没有的商品
                # ============================================================
                if cart_product_groups:
                    for product_id, group in cart_product_groups.items():
                        messages.append(
                            f"购物车中有订单外的商品: {group.get('name', '未知')} "
                            f"(数量: {group.get('count', 0)})"
                        )
                
                if messages:
                    if attempt < max_retry - 1:
                        self.logger.warning(
                            f"购物车数据不一致 (尝试 {attempt + 1}/{max_retry})，"
                            f"刷新页面重试"
                        )
                        time.sleep(retry_interval)
                        continue
                    else:
                        message = "购物车数据不一致:\n" + "\n".join(messages)
                        return False, message
                
                # 数据一致
                self.logger.info("购物车数据验证通过")
                return True, ""
            
            except Exception as e:
                if attempt < max_retry - 1:
                    self.logger.warning(
                        f"验证购物车数据时出错 (尝试 {attempt + 1}/{max_retry}): {e}"
                    )
                    time.sleep(retry_interval)
                else:
                    self.logger.error(f"验证购物车数据失败: {e}")
                    return False, f"验证购物车数据失败: {e}"
        
        return False, "购物车数据验证失败，已达到最大重试次数"

