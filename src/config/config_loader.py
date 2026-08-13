"""
配置加载和管理模块
"""

import yaml
import os
from typing import Dict, Any
from pathlib import Path


class ConfigLoader:
    """配置加载器"""
    
    _config: Dict[str, Any] = None
    _config_path: str = None
    
    @classmethod
    def load_config(cls, config_path: str = None) -> Dict[str, Any]:
        """
        加载配置文件
        
        Args:
            config_path: 配置文件路径，默认为项目根目录下的config.yaml
            
        Returns:
            配置字典
        """
        if config_path is None:
            # 获取项目根目录
            project_root = Path(__file__).parent.parent.parent
            config_path = project_root / "config.yaml"
        
        config_path = str(config_path)
        cls._config_path = config_path
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                cls._config = yaml.safe_load(f)
            
            # 验证必要的配置项
            cls._validate_config(cls._config)
            
            return cls._config
        
        except yaml.YAMLError as e:
            raise ValueError(f"配置文件格式错误: {e}")
        except Exception as e:
            raise RuntimeError(f"加载配置文件失败: {e}")
    
    @classmethod
    def get_config(cls) -> Dict[str, Any]:
        """获取已加载的配置"""
        if cls._config is None:
            cls.load_config()
        return cls._config
    
    @classmethod
    def _validate_config(cls, config: Dict[str, Any]):
        """
        验证配置文件的必要项
        
        Args:
            config: 配置字典
        """
        required_sections = [
            'browser',
            'scheduler',
            'order_api',
            'ticket_api',
            'feishu_webhook',
            'logging'
        ]
        
        for section in required_sections:
            if section not in config:
                raise ValueError(f"配置文件中缺少必要的配置项: {section}")
        
        # 验证浏览器配置
        if 'chrome_path' not in config['browser']:
            raise ValueError("配置文件中缺少浏览器路径配置: browser.chrome_path")
        
        # 验证日志配置
        if 'log_dir' not in config['logging']:
            config['logging']['log_dir'] = 'logs'
        
        # 创建必要的目录
        log_dir = config['logging'].get('log_dir', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        
        # 创建数据目录（如果配置了数据库）
        if 'database' in config and 'sqlite_path' in config['database']:
            db_path = config['database']['sqlite_path']
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        
        # 创建浏览器用户数据目录（如果配置了）
        if 'browser' in config and 'user_data_dir' in config['browser']:
            user_data_dir = config['browser'].get('user_data_dir', '').strip()
            if user_data_dir:
                if not os.path.isabs(user_data_dir):
                    # 相对路径，从项目根目录创建
                    project_root = Path(__file__).parent.parent.parent
                    user_data_dir = project_root / user_data_dir
                else:
                    user_data_dir = Path(user_data_dir)
                os.makedirs(user_data_dir, exist_ok=True)
    
    @classmethod
    def reload_config(cls) -> Dict[str, Any]:
        """重新加载配置文件"""
        cls._config = None
        return cls.load_config(cls._config_path)

