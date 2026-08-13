# -*- coding: utf-8 -*-
"""
定时任务调度器
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from src.utils.logger import LoggerMixin


class TaskScheduler(LoggerMixin):
    """定时任务调度器"""

    def __init__(
        self,
        interval_minutes: int = 15,
        start_delay_seconds: int = 5,
        config: Optional[Dict[str, Any]] = None,
    ):
        """
        初始化调度器

        Args:
            interval_minutes: 任务执行间隔（分钟）
            start_delay_seconds: 启动延迟（秒）
            config: 站点合并配置（含 _log_namespace 时日志写入 site.*）
        """
        self.interval_minutes = max(1, int(interval_minutes or 15))
        self.start_delay_seconds = max(0, int(start_delay_seconds or 0))
        self.config = config or {}
        self.last_success_time: Optional[datetime] = None
        self.last_finish_time: Optional[datetime] = None
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        self.task_callback: Optional[Callable] = None

    def set_task(self, task_func: Callable):
        """
        设置要执行的任务函数

        Args:
            task_func: 任务函数，应该返回True表示成功，False表示失败
        """
        self.task_callback = task_func

    def start(self):
        """启动调度器"""
        if self.is_running:
            self.logger.warning("调度器已经在运行中")
            return

        self.is_running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.logger.info(
            "定时任务调度器已启动：间隔 %s 分钟，启动延迟 %s 秒",
            self.interval_minutes,
            self.start_delay_seconds,
        )

    def stop(self):
        """停止调度器"""
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=5)
        self.logger.info("定时任务调度器已停止")

    def _run(self):
        """调度器主循环"""
        if self.start_delay_seconds > 0:
            self.logger.info(
                "调度启动延迟 %s 秒后执行首轮任务", self.start_delay_seconds
            )
            time.sleep(self.start_delay_seconds)

        while self.is_running:
            try:
                if self.task_callback is None:
                    self.logger.error("未设置任务回调函数")
                    break

                self.logger.info("=" * 60)
                self.logger.info("开始执行定时任务")
                self.logger.info("=" * 60)

                start_time = datetime.now()
                success = False

                try:
                    result = self.task_callback()
                    success = result if isinstance(result, bool) else True
                except Exception as e:
                    self.logger.error("任务执行异常: %s", e, exc_info=True)
                    success = False

                end_time = datetime.now()
                self.last_finish_time = end_time
                duration = (end_time - start_time).total_seconds()

                if success:
                    self.last_success_time = end_time
                    self.logger.info("任务执行成功，耗时: %.2f秒", duration)
                else:
                    self.logger.warning("任务执行失败，耗时: %.2f秒", duration)

                # 无论成败，均从本轮结束起等待固定间隔
                wait_seconds = float(self.interval_minutes) * 60.0
                next_time = end_time + timedelta(seconds=wait_seconds)
                self.logger.info(
                    "本轮结束，等待 %s 分钟后再次拉单（预计 %s）",
                    self.interval_minutes,
                    next_time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                while wait_seconds > 0 and self.is_running:
                    sleep_time = min(wait_seconds, 10)
                    time.sleep(sleep_time)
                    wait_seconds -= sleep_time

            except Exception as e:
                self.logger.error("调度器运行异常: %s", e, exc_info=True)
                if self.is_running:
                    time.sleep(60)

    def get_next_execution_time(self) -> Optional[datetime]:
        """获取下次执行时间。"""
        base = self.last_finish_time or self.last_success_time
        if base:
            return base + timedelta(minutes=self.interval_minutes)
        return None

    def get_status(self) -> dict:
        """获取调度器状态。"""
        nxt = self.get_next_execution_time()
        return {
            "is_running": self.is_running,
            "interval_minutes": self.interval_minutes,
            "last_success_time": (
                self.last_success_time.isoformat() if self.last_success_time else None
            ),
            "next_execution_time": nxt.isoformat() if nxt else None,
        }
