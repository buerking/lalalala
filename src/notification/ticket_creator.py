"""
工单创建模块
"""

import requests
from typing import Dict, Any, List
import logging

from src.utils.logger import LoggerMixin
from src.utils.retry import retry


class TicketCreator(LoggerMixin):
    """工单创建器"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化工单创建器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.ticket_config = config.get('ticket_api', {})
        self.base_url = self.ticket_config.get('base_url', '')
        self.endpoint = self.ticket_config.get('endpoint', '/api/tickets')
        self.headers = self.ticket_config.get('headers', {})
        self.timeout = self.ticket_config.get('timeout', 30)
    
    @retry(max_attempts=3, delay=5.0)
    def create_ticket(self, order_id: str, messages: List[str], user_id: str | None = None):
        """
        创建用户工单
        
        Args:
            order_id: 订单ID
            messages: 消息列表
            user_id: 用户ID（如果为空，将尝试从配置中读取默认用户ID）
        """
        if not self.base_url:
            self.logger.warning("工单API基础URL未配置，跳过工单创建")
            # 为方便排查问题，仍然在日志中输出工单内容
            ticket_content = self._format_ticket_content(order_id, messages, user_id or "未知用户")
            self.logger.info(f"（仅日志预览，未实际请求）工单内容（订单 {order_id}）:\n{ticket_content}")
            return
        
        # 决定用户ID（优先函数参数，其次配置）
        if not user_id:
            user_id = self.ticket_config.get('default_user_id', '未知用户')
        
        try:
            # 1. 构造工单标题与描述
            ticket_content = self._format_ticket_content(order_id, messages, user_id)
            title = f"尊敬的用户【{user_id}】您的代购订单【{order_id}】有问题，需要您手动确认"
            
            # 2. 拼接请求URL
            url = f"{self.base_url.rstrip('/')}/{self.endpoint.lstrip('/')}"
            
            # 3. 按约定格式构建POST请求体
            ticket_data = {
                "userId": user_id,
                "orderId": order_id,
                "title": title,
                "description": ticket_content,
                # 预留原始问题列表，方便后端做结构化处理（可选）
                "rawMessages": messages,
            }
            
            self.logger.info(f"开始创建工单，请求URL: {url}")
            self.logger.debug(f"工单请求体: {ticket_data}")
            
            response = requests.post(
                url,
                json=ticket_data,
                headers=self.headers,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            self.logger.info(f"工单创建成功（订单 {order_id}，用户 {user_id}），响应状态码: {response.status_code}")
        
        except Exception as e:
            self.logger.error(f"创建工单失败: {e}")
            raise
    
    def _format_ticket_content(self, order_id: str, messages: List[str], user_id: str) -> str:
        """
        格式化工单内容
        
        Args:
            order_id: 订单ID
            messages: 消息列表（通常每条包含“链接 + 描述”）
            user_id: 用户ID
            
        Returns:
            格式化后的工单内容（作为工单描述）
        """
        content_lines = [
            f"用户ID: {user_id}",
            f"订单ID: {order_id}",
            "",
            f"订单 {order_id} 处理过程中发现问题:",
            ""
        ]
        
        for msg in messages:
            # 每条问题前面增加固定前缀，方便客服阅读
            content_lines.append(f"对应的订单链接：{msg}")
        
        return "\n".join(content_lines)

