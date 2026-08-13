#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
订单自动化处理系统 - 主程序入口
"""

import sys
import os

# 将项目根目录加入路径，使 `import src.xxx` 可用（包在根目录下的 src/ 文件夹内）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.gui.main_window import MainWindow
from src.config.config_loader import ConfigLoader
from src.utils.logger import setup_logger
import tkinter as tk


def main():
    """主函数"""
    try:
        # 加载配置
        config = ConfigLoader.load_config()
        
        # 设置日志
        logger = setup_logger(config)
        logger.info("=" * 60)
        logger.info("订单自动化处理系统启动")
        logger.info("=" * 60)
        
        # 创建GUI窗口
        root = tk.Tk()
        app = MainWindow(root, config, logger)
        
        # 运行GUI主循环
        root.mainloop()
        
    except Exception as e:
        print(f"程序启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
