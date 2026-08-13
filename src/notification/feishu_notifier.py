"""
飞书通知模块
飞书机器人 Webhook v2 要求：msg_type + content，见 https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN
"""

import requests
import base64
from typing import Dict, Any, Optional, List

from src.utils.logger import LoggerMixin
from src.utils.retry import retry


class FeishuNotifier(LoggerMixin):
    """飞书通知器"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化飞书通知器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.webhook_config = config.get('feishu_webhook', {})
        self.webhook_url = self.webhook_config.get('url', '')
        # PayPay 扫码专用群（可选）：用于发送扫码准备/二维码，未配置时回退默认 webhook
        self.paypay_scan_webhook_url = self.webhook_config.get('paypay_scan_url', '')
        self.enabled = self.webhook_config.get('enabled', True)

    def _resolve_webhook_url(self, use_paypay_scan_webhook: bool = False) -> str:
        if use_paypay_scan_webhook and self.paypay_scan_webhook_url:
            return self.paypay_scan_webhook_url
        return self.webhook_url

    def _post_webhook(self, body: Dict[str, Any], use_paypay_scan_webhook: bool = False) -> None:
        """统一 POST 到 webhook，校验 v2 要求的 msg_type / content。"""
        if not self.enabled:
            return
        webhook_url = self._resolve_webhook_url(use_paypay_scan_webhook=use_paypay_scan_webhook)
        if not webhook_url:
            self.logger.warning("飞书Webhook URL未配置")
            return
        response = requests.post(webhook_url, json=body, timeout=10)
        response.raise_for_status()
        ret = response.json()
        if ret.get("code") and ret["code"] != 0:
            raise RuntimeError(f"飞书返回错误: {ret.get('msg', ret)}")
    
    @retry(max_attempts=3, delay=2.0)
    def send_message(self, title: str, content: str, use_paypay_scan_webhook: bool = False):
        """
        发送文本/卡片消息到飞书（v2：msg_type + content）
        
        Args:
            title: 消息标题
            content: 消息内容（支持 markdown 式排版）
        """
        if not self.enabled:
            self.logger.debug("飞书通知已禁用")
            return
        webhook_url = self._resolve_webhook_url(use_paypay_scan_webhook=use_paypay_scan_webhook)
        if not webhook_url:
            self.logger.warning("飞书Webhook URL未配置")
            return
        try:
            # 飞书 v2 要求 msg_type + content；先尝试卡片，失败则降级为纯文本
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "red"
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": content}}
                ]
            }
            message = {"msg_type": "interactive", "card": card}
            try:
                self._post_webhook(message, use_paypay_scan_webhook=use_paypay_scan_webhook)
            except Exception as card_err:
                # 若卡片格式不被接受，用纯文本（v2 必选格式之一）
                text_body = {"msg_type": "text", "content": {"text": f"{title}\n\n{content}"}}
                self._post_webhook(text_body, use_paypay_scan_webhook=use_paypay_scan_webhook)
                self.logger.debug("已降级为文本消息: %s", card_err)
            self.logger.info(f"飞书消息发送成功: {title}")
        except Exception as e:
            self.logger.error(f"发送飞书消息失败: {e}")
            raise
    
    @retry(max_attempts=3, delay=2.0)
    def notify_order_issue(
        self,
        order_id: str,
        messages: List[str],
        user_id: Optional[str] = None,
        extra: Optional[str] = None,
    ):
        """
        自动下单流程出现问题时的飞书群提醒，用于通知人工处理。
        
        Args:
            order_id: 订单ID
            messages: 问题列表（如库存不足、价格变动、页面超时等）
            user_id: 用户ID（若有，来自订单接口）
            extra: 额外说明（如「已创建工单」）
        """
        if not self.enabled:
            self.logger.debug("飞书通知已禁用，跳过订单异常提醒")
            return
        if not self.webhook_url:
            self.logger.warning("飞书Webhook URL未配置，跳过订单异常提醒")
            return
        try:
            title = "【代购】自动下单异常，请人工处理"
            lines = [
                f"**订单ID**: {order_id}",
                "",
            ]
            if user_id:
                lines.append(f"**用户ID**: {user_id}")
                lines.append("")
            lines.append("**问题摘要**:")
            for i, msg in enumerate(messages[:20], 1):  # 最多 20 条
                # 飞书 lark_md 中换行用 \n，长链接可折叠
                safe_msg = msg.replace("\n", " ").strip()
                lines.append(f"{i}. {safe_msg}")
            if len(messages) > 20:
                lines.append(f"... 等共 {len(messages)} 条")
            if extra:
                lines.append("")
                lines.append(extra)
            content = "\n".join(lines)
            self.send_message(title, content)
            self.logger.info(f"已发送飞书提醒: 订单 {order_id} 需人工处理")
        except Exception as e:
            self.logger.error(f"发送飞书订单异常提醒失败: {e}")
            raise

    # PayPay 队列入队原因（用于飞书文案）
    PAYPAY_QUEUE_REASON_LABELS = {
        "tenpo_branch": "第三方店铺 / 分店（tenpo_cd）商品",
    }

    @retry(max_attempts=3, delay=2.0)
    def notify_paypay_queue_enqueued(
        self,
        order_id: str,
        reason_code: str,
        messages: List[str],
        user_id: Optional[str] = None,
        order_no: Optional[str] = None,
    ) -> None:
        """
        订单写入 PayPay 扫码队列后的飞书通知（发往默认预警群）。
        扫码群（paypay_scan_url）仅用于发送二维码/扫码页截图。

        Args:
            order_id: 订单 ID
            reason_code: r18 | presale | tenpo_branch（见 PAYPAY_QUEUE_REASON_LABELS）
            messages: 简要说明条目（如命中商品链接与名称）
            user_id: 用户 ID（可选）
            order_no: 订单号（可选，拉单场景便于核对）
        """
        if not self.enabled:
            self.logger.debug("飞书通知已禁用，跳过 PayPay 队列入队提醒")
            return
        if not self._resolve_webhook_url(use_paypay_scan_webhook=False):
            self.logger.warning("飞书 Webhook 未配置，跳过 PayPay 队列入队提醒")
            return
        label = self.PAYPAY_QUEUE_REASON_LABELS.get(reason_code, reason_code)
        title = "【PayPay队列】骏河屋订单已入队"
        lines = [
            f"**订单ID**: {order_id}",
        ]
        if order_no:
            lines.append(f"**订单号**: {order_no}")
        if user_id:
            lines.append(f"**用户ID**: {user_id}")
        lines.append("")
        lines.append(f"**入队原因**: {label}")
        lines.append("")
        lines.append("**详情**:")
        for i, msg in enumerate(messages[:20], 1):
            safe_msg = msg.replace("\n", " ").strip()
            lines.append(f"{i}. {safe_msg}")
        if len(messages) > 20:
            lines.append(f"... 等共 {len(messages)} 条")
        lines.append("")
        lines.append(
            "**后续**: 本单已写入 PayPay 扫码队列；将在配置的扫码时间段内自动处理，"
            "或于系统停止时点击「处理PayPay队列」手动处理。"
        )
        content = "\n".join(lines)
        try:
            self.send_message(title, content, use_paypay_scan_webhook=False)
            self.logger.info("已发送 PayPay 队列入队飞书(预警群): 订单 %s (%s)", order_id, reason_code)
        except Exception as e:
            self.logger.error(f"发送 PayPay 队列入队飞书失败: {e}")
            raise

    @retry(max_attempts=3, delay=2.0)
    def send_qr_code(self, order_id: str, qr_code_image: bytes):
        """
        发送二维码图片到飞书
        
        Args:
            order_id: 订单ID
            qr_code_image: 二维码图片的字节数据
        """
        if not self.enabled:
            self.logger.debug("飞书通知已禁用")
            return
        
        if not self.webhook_url:
            self.logger.warning("飞书Webhook URL未配置")
            return
        
        try:
            # 将图片转换为base64
            image_base64 = base64.b64encode(qr_code_image).decode('utf-8')
            
            # 飞书支持图片消息，但需要先上传图片获取image_key
            # 这里使用简化方式，发送文本消息包含订单ID
            # TODO: 如果需要发送图片，需要调用飞书图片上传API
            
            title = f"订单支付二维码 - {order_id}"
            content = f"**订单ID**: {order_id}\n\n请扫描支付二维码完成支付。\n\n⚠️ 注意：二维码图片功能需要配置飞书图片上传API"
            
            self.send_message(title, content)
            
            self.logger.info(f"二维码通知已发送到飞书，订单: {order_id}")
        
        except Exception as e:
            self.logger.error(f"发送二维码到飞书失败: {e}")
            raise

