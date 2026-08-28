"""
订单处理器
"""

from typing import Dict, Any, List, Optional, Tuple
import logging

from src.utils.logger import LoggerMixin
from src.browser.browser_manager import BrowserManager
from src.browser.page_handler import PageHandler
from src.cart.cart_verifier import CartVerifier
from src.payment.payment_handler import PaymentHandler
from src.notification.ticket_creator import TicketCreator
from src.notification.feishu_notifier import FeishuNotifier
from src.order.added_cart_callback import send_added_cart_callback
from src.utils.paypay_queue import enqueue_paypay_order


class OrderProcessor(LoggerMixin):
    """订单处理器"""
    
    def __init__(self, config: Dict[str, Any], browser_manager: BrowserManager):
        """
        初始化订单处理器
        
        Args:
            config: 配置字典
            browser_manager: 浏览器管理器实例
        """
        self.config = config
        self.browser_manager = browser_manager
        self.page_handler = PageHandler(browser_manager, config)
        self.cart_verifier = CartVerifier(browser_manager, config)
        self.payment_handler = PaymentHandler(browser_manager, config)
        self.ticket_creator = TicketCreator(config)
        self.feishu_notifier = FeishuNotifier(config)

    def _handle_order_issue(self, order: Dict[str, Any], messages: List[str], reason: str = "") -> bool:
        """
        统一封装：为有问题的订单创建工单 + 发送飞书通知。
        
        Args:
            order: 订单字典（至少包含 order_id / user_id）
            messages: 问题列表
            reason: 触发原因简要说明（例如 '清空购物车失败'、'商品处理异常'、'购物车验证失败'）
        Returns:
            False（方便调用处直接 `return self._handle_order_issue(...)`）
        """
        order_id = order.get("order_id", "未知")
        user_id = order.get("user_id")
        if not messages:
            return False
        
        # 统一日志输出
        if reason:
            self.logger.warning(f"订单 {order_id} 处理过程中发现问题（{reason}）:")
        else:
            self.logger.warning(f"订单 {order_id} 处理过程中发现问题:")
        for msg in messages:
            self.logger.warning(f"  - {msg}")
        
        # 创建工单
        try:
            self.ticket_creator.create_ticket(order_id, messages, user_id=user_id)
            self.logger.info(f"已为订单 {order_id} 创建工单")
        except Exception as e:
            self.logger.error(f"创建工单失败: {e}")
        
        # 飞书提醒（失败不影响主流程）
        try:
            extra = "已创建工单，请及时处理。" if not reason else f"{reason}，已创建工单，请及时处理。"
            self.feishu_notifier.notify_order_issue(order_id, messages, user_id=user_id, extra=extra)
        except Exception as feishu_err:
            self.logger.warning(f"飞书提醒发送失败: {feishu_err}")
        
        return False
    
    def _make_order_summary(
        self,
        order: Dict[str, Any],
        success: bool = False,
        failure_reason: str = "",
        payment_method: str = "",
        check_cart_requested: bool = False,
        check_cart_response: str = "未请求",
        add_no_requested: bool = False,
        add_no_response: str = "未请求",
        update_errors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """构造 is_success.log 用的本单汇总结构（与 payment_handler 的 summary 一致）。"""
        return {
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
    
    def process_order(self, order: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        处理单个订单的完整流程
        
        Args:
            order: 订单数据字典，包含 order_id、order_no、products 等信息
            
        Returns:
            (success, summary) 供 is_success.log 汇总使用
        """
        order_id = order.get('order_id', '未知')
        products = order.get('products', [])

        adapter = (self.config.get("_site") or {}).get("adapter") or "surugaya"
        if adapter == "yahoo_fleamarket":
            from src.order.yahoo_fleamarket_processor import YahooFleaMarketOrderProcessor

            yp = YahooFleaMarketOrderProcessor(self.config, self.browser_manager)
            return yp.process_order(order)
        
        self.logger.info(f"开始处理订单: {order_id}")
        self.logger.info(f"订单包含 {len(products)} 个商品")
        
        # 收集消息
        messages: List[str] = []
        
        try:
            # 步骤：每个订单开始前先清空购物车
            # 重要：清空购物车失败会导致数据污染，必须成功后才能继续
            self.logger.info("清空购物车，准备处理新订单")
            try:
                self.cart_verifier.clear_cart()
                self.logger.info("购物车已清空，开始处理订单商品")
            except Exception as e:
                self.logger.error(f"清空购物车失败（已重试），无法继续处理订单: {e}")
                messages.append(f"清空购物车失败: {e}")
                self._handle_order_issue(order, messages, reason="清空购物车失败")
                return False, self._make_order_summary(order, failure_reason="清空购物车失败: %s" % e)
            
            # 步骤3-8: 循环处理每个商品
            for idx, product in enumerate(products, 1):
                self.logger.info(f"处理商品 {idx}/{len(products)}: {product.get('name', '未知')}")
                
                product_url = product.get('url')
                order_price = product.get('price', 0)
                order_quantity = product.get('quantity', 0)
                
                if not product_url:
                    messages.append(f"商品 {product.get('name', '未知')} 缺少链接")
                    continue
                
                # 打开商品页面
                try:
                    self.page_handler.open_product_page(product_url)
                except Exception as e:
                    self.logger.error(f"打开商品页面失败: {e}")
                    messages.append(f"商品 {product.get('name')} 页面打开失败: {e}")
                    continue
                
                # R18 检测：与预售一致，整单写入 PayPay 队列并发飞书，不创建工单
                try:
                    if self.page_handler.is_r18_product():
                        r18_msg = (
                            f"{product_url} 商品 {product.get('name')} 为 R18 商品，"
                            f"已写入 PayPay 队列，本单不再自动加购/支付"
                        )
                        self.logger.warning(r18_msg)
                        product['is_r18'] = True
                        try:
                            enqueue_paypay_order(self.config, order)
                            self.logger.info(
                                "订单 %s 含 R18 商品，已写入 PayPay 队列",
                                order_id,
                            )
                        except Exception as q_err:
                            self.logger.warning("R18 订单写入 PayPay 队列失败: %s", q_err)
                        try:
                            self.feishu_notifier.notify_paypay_queue_enqueued(
                                order_id,
                                "r18",
                                [r18_msg],
                                user_id=order.get("user_id"),
                                order_no=str(order.get("order_no") or "") or None,
                            )
                        except Exception as feishu_err:
                            self.logger.warning(f"飞书提醒发送失败(R18): {feishu_err}")
                        return False, self._make_order_summary(
                            order, False, "订单包含R18商品，已写入 PayPay 队列"
                        )
                    else:
                        product['is_r18'] = False
                except Exception as e:
                    self.logger.warning(f"检测 R18 状态失败，按非R18处理: {e}")

                # 预售检测：参考 R18，终止本单自动流程；订单写入 PayPay 队列并发飞书（不创建工单）
                try:
                    if self.page_handler.is_presale_product():
                        presale_msg = (
                            f"{product_url} 商品 {product.get('name')} 为预售（未発売）商品，"
                            f"已写入 PayPay 队列，本单不再自动加购/支付"
                        )
                        self.logger.warning(presale_msg)
                        product["is_presale"] = True
                        try:
                            enqueue_paypay_order(self.config, order)
                            self.logger.info(
                                "订单 %s 含预售商品，已写入 PayPay 队列",
                                order_id,
                            )
                        except Exception as q_err:
                            self.logger.warning("预售订单写入 PayPay 队列失败: %s", q_err)
                        try:
                            self.feishu_notifier.notify_paypay_queue_enqueued(
                                order_id,
                                "presale",
                                [presale_msg],
                                user_id=order.get("user_id"),
                                order_no=str(order.get("order_no") or "") or None,
                            )
                        except Exception as feishu_err:
                            self.logger.warning("飞书提醒发送失败(预售): %s", feishu_err)
                        return False, self._make_order_summary(
                            order, False, "订单包含预售商品，已写入 PayPay 队列"
                        )
                    else:
                        product["is_presale"] = False
                except Exception as e:
                    self.logger.warning("检测预售状态失败，按非预售处理: %s", e)

                # 检查库存
                stock_ok, stock_msg = self.page_handler.check_stock(product_url, order_quantity)
                if not stock_ok:
                    product_name = product.get('name', '未知商品')
                    messages.append(f"商品名称: {product_name} | 链接: {product_url} | {stock_msg}")
                    self.logger.warning(stock_msg)
                    continue
                
                # 检查单价（价格变动不拦截：不记入 messages、不跳过加购，后续由后端接口更新订单信息为准）
                price_ok, price_msg = self.page_handler.check_price(product_url, order_price)
                if not price_ok:
                    self.logger.warning(price_msg)
                    # 不 append 到 messages、不 continue，继续执行加购及后续流程
                
                # 加入购物车
                try:
                    self.page_handler.add_to_cart(product_url, order_quantity)
                    self.logger.info(f"商品 {product.get('name')} 已加入购物车（数量: {order_quantity}）")
                    # 加购成功后必须成功回调 addedCartCallbackSimple，否则停止后续、跳过本单并飞书
                    use_curl = (self.config.get('order_api') or {}).get('use_curl_for_order_api', True)
                    try:
                        callback_ok, cb_msg = send_added_cart_callback(
                            order, product,
                            config=self.config,
                            is_lack=0,
                            is_limit=0,
                            use_curl=use_curl,
                        )
                    except Exception as cb_err:
                        self.logger.error("加购回调请求异常: %s", cb_err)
                        messages.append(f"加购回调 addedCartCallbackSimple 请求异常: {cb_err}")
                        self._handle_order_issue(order, messages, reason="加购回调失败")
                        return False, self._make_order_summary(order, failure_reason="加购回调请求异常: %s" % cb_err)
                    if not callback_ok:
                        self.logger.error(
                            "加购回调 addedCartCallbackSimple 未成功 Message=%s",
                            cb_msg or "-",
                        )
                        messages.append(
                            "加购回调 addedCartCallbackSimple 未成功（Message=%s）"
                            % (cb_msg or "-")
                        )
                        self._handle_order_issue(order, messages, reason="加购回调失败")
                        return False, self._make_order_summary(
                            order,
                            failure_reason="加购回调 addedCartCallbackSimple 未成功: %s"
                            % (cb_msg or "-"),
                        )
                except (ValueError, NotImplementedError) as e:
                    # 选择器未配置或功能未实现，记录消息并继续
                    error_msg = str(e)
                    self.logger.warning(f"加入购物车跳过: {error_msg}")
                    messages.append(f"{product_url} 商品 {product.get('name')} 加入购物车失败: {error_msg}")
                    continue
                except Exception as e:
                    self.logger.error(f"加入购物车失败: {e}")
                    messages.append(f"{product_url} 商品 {product.get('name')} 加入购物车失败: {e}")
                    continue
            
            # 步骤9: 判断是否有message
            if messages:
                # 商品处理过程中出现问题（库存不足 / 价格变动 / 页面异常 / R18 等）
                self._handle_order_issue(order, messages, reason="商品处理异常")
                return False, self._make_order_summary(order, failure_reason="商品处理异常: %s" % "; ".join(messages[:5]))
            
            # 步骤10-12: 验证购物车数据（仅作参考日志，不做强校验拦截；最终以后端接口校验为准）
            try:
                self.logger.info("开始验证购物车数据")
                cart_ok, cart_msg = self.cart_verifier.verify_cart_data(order)
                if not cart_ok:
                    # 仅记录 warning，方便人工排查，不拦截后续流程
                    self.logger.warning(f"购物车数据验证未通过（仅记录日志，不拦截）: {cart_msg}")
            except Exception as e:
                # 购物车页面结构变化等异常也不拦截，只记日志
                self.logger.warning(f"购物车数据验证过程中发生异常（已忽略，不拦截）: {e}")
            
            # 步骤13-16: 执行支付流程（传入完整 order 以便按商品标记选择支付方式）
            self.logger.info("开始执行支付流程")
            payment_success, payment_summary = self.payment_handler.process_payment(order)
            
            if not payment_success:
                self.logger.error(f"订单 {order_id} 支付流程失败")
                return False, payment_summary
            
            self.logger.info(f"订单 {order_id} 处理完成")
            return True, payment_summary
        
        except Exception as e:
            self.logger.error(f"处理订单 {order_id} 时发生异常: {e}", exc_info=True)
            return False, self._make_order_summary(order, failure_reason="处理异常: %s" % e)

