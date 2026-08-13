"""
日志管理模块
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Any


def setup_logger(config: Dict[str, Any]) -> logging.Logger:
    """
    设置日志系统
    
    Args:
        config: 配置字典
        
    Returns:
        配置好的Logger实例
    """
    log_config = config.get('logging', {})
    
    # 创建日志目录
    log_dir = log_config.get('log_dir', 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # 创建Logger
    logger = logging.getLogger('OrderProcessor')
    logger.setLevel(getattr(logging, log_config.get('level', 'INFO')))
    
    # 清除已有的处理器
    logger.handlers.clear()
    
    # 文件处理器（带轮转）
    log_file = log_config.get('log_file', 'order_processor.log')
    log_path = os.path.join(log_dir, log_file)
    
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=log_config.get('max_file_size_mb', 10) * 1024 * 1024,
        backupCount=log_config.get('backup_count', 5),
        encoding='utf-8'
    )
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 控制台处理器（如果启用）
    if log_config.get('console_output', True):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    return logger


class LoggerMixin:
    """日志混入类，方便其他类使用日志"""
    
    @property
    def logger(self) -> logging.Logger:
        """获取Logger实例"""
        cfg = getattr(self, "config", None)
        if isinstance(cfg, dict):
            ns = (cfg.get("_log_namespace") or "").strip()
            if ns:
                return logging.getLogger(f"site.{ns}")
        return logging.getLogger(self.__class__.__name__)

