# -*- coding: utf-8 -*-
"""单站点：浏览器、调度、订单处理、锁；可与 GUI 控件绑定。"""

from __future__ import annotations

import copy
import logging
import os
import random
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.browser.browser_manager import BrowserManager
from src.config.site_merge import merge_site_config
from src.order.order_fetcher import OrderFetcher
from src.order.order_processor import OrderProcessor
from src.scheduler.task_scheduler import TaskScheduler
from src.utils.site_logging import attach_site_file_logger, detach_site_handlers


def _subprocess_run_text_kw() -> Dict[str, Any]:
    """Windows 下 PowerShell 输出可能含非 UTF-8 字节，避免 decode 崩掉 reader 线程。"""
    return {"text": True, "encoding": "utf-8", "errors": "replace"}


class SiteRunner:
    def __init__(
        self,
        root,
        global_config: Dict[str, Any],
        site_definition: Optional[Dict[str, Any]] = None,
        *,
        legacy_single: bool = False,
    ):
        """
        Args:
            root: Tk root（用于 after 回调）
            global_config: 完整配置
            site_definition: 站点覆盖 dict；legacy_single=True 时忽略
            legacy_single: True 时使用 global_config 原样（不合并、不写 _site）
        """
        self.root = root
        self.global_config = global_config
        self.legacy_single = legacy_single
        if legacy_single:
            self.merged_config = global_config
            self.site_id = "default"
            self.adapter = "surugaya"
            self.display_name = "骏河屋"
            self.manual_login_url = "https://www.suruga-ya.jp/"
        else:
            self.merged_config = merge_site_config(global_config, site_definition or {})
            meta = self.merged_config.get("_site") or {}
            self.site_id = meta.get("id") or "site"
            self.adapter = meta.get("adapter") or "surugaya"
            self.display_name = meta.get("display_name") or self.site_id
            self.manual_login_url = (meta.get("manual_login_url") or "").strip()

        self.browser_manager: Optional[BrowserManager] = None
        self.scheduler: Optional[TaskScheduler] = None
        self.is_running: bool = False
        # 自动批处理 / 手动 PayPay 队列 互斥：
        # - 自动进行中：手动立即拒绝
        # - 手动进行中：到点的自动任务阻塞等待，结束后再跑本轮
        # - 上一轮自动仍在跑：下一轮自动跳过（避免堆积）
        self._processing_lock = threading.Lock()
        self._busy_state_lock = threading.Lock()
        self._busy_owner: Optional[str] = None  # "auto" | "manual" | None
        self._browser_lock = threading.Lock()
        # self._paused = False 取消掉暂停，保持继续调度执行
        self.ui_log: Optional[Callable[[str], None]] = None

    def _get_busy_owner(self) -> Optional[str]:
        with self._busy_state_lock:
            return self._busy_owner

    def _set_busy_owner(self, owner: Optional[str]) -> None:
        with self._busy_state_lock:
            self._busy_owner = owner

    def _begin_auto_batch(self) -> bool:
        """
        申请自动批处理锁。
        Returns:
            True 已持锁；False 应跳过本轮（上一轮自动尚未结束）。
        """
        if self._processing_lock.acquire(blocking=False):
            self._set_busy_owner("auto")
            return True

        owner = self._get_busy_owner()
        if owner == "auto":
            # 明确是上一轮自动未结束：跳过，避免定时任务堆积
            self._logger().warning("上一轮自动订单处理尚未结束，跳过本轮定时任务")
            return False

        # manual 占用，或 owner 与锁短暂不同步：阻塞等待，结束后再跑本轮
        if owner == "manual":
            self._logger().info(
                "手动 PayPay 队列处理中，自动定时任务等待其结束后再执行…"
            )
        else:
            self._logger().info("处理锁暂忙，自动定时任务等待…")
        self._processing_lock.acquire(blocking=True)
        self._set_busy_owner("auto")
        self._logger().info("已获得处理锁，开始执行本轮自动任务")
        return True

    def _begin_manual_batch(self) -> bool:
        """
        申请手动 PayPay 队列锁（非阻塞）。
        Returns:
            True 已持锁；False 当前自动/其他手动占用，应拒绝。
        """
        if self._processing_lock.acquire(blocking=False):
            self._set_busy_owner("manual")
            return True
        owner = self._get_busy_owner() or "订单处理"
        label = {
            "auto": "自动订单批处理",
            "manual": "另一轮手动 PayPay 队列",
        }.get(owner, owner)
        self._logger().warning(
            "当前正在%s，无法开始手动处理 PayPay 队列（请等待结束后再点）",
            label,
        )
        return False

    def _end_batch(self) -> None:
        self._set_busy_owner(None)
        try:
            self._processing_lock.release()
        except Exception:
            pass

    def _keep_browser_open_on_stop(self) -> bool:
        browser_cfg = self.merged_config.get("browser") or {}
        return bool(browser_cfg.get("keep_browser_open_on_stop", True))

    @staticmethod
    def _parse_float_range(v: Any, default_min: float, default_max: float) -> Tuple[float, float]:
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                a = float(v[0])
                b = float(v[1])
                return (min(a, b), max(a, b))
            except Exception:
                pass
        return (default_min, default_max)

    def _sleep_order_cooldown_if_needed(self) -> None:
        pay_cfg = self.merged_config.get("payment") or {}
        mn, mx = self._parse_float_range(
            pay_cfg.get("order_end_cooldown_seconds_range"),
            1.5,
            4.0,
        )
        if mx <= 0:
            return
        sec = random.uniform(max(0.0, mn), max(0.0, mx))
        self._logger().info("本单结束后随机冷却 %.2f 秒", sec)
        time.sleep(sec)

    @staticmethod
    def _normalize_profile_path(path: str) -> str:
        """规范化 profile 目录，便于跨斜杠/大小写精确比较。"""
        if not path:
            return ""
        p = path.strip().strip('"').strip("'")
        if not p:
            return ""
        return os.path.normcase(os.path.normpath(p)).rstrip("\\/")

    @classmethod
    def _extract_user_data_dirs_from_cmdline(cls, cmd: str) -> List[str]:
        """
        从进程命令行提取 --user-data-dir 的值。
        支持: --user-data-dir=PATH / --user-data-dir="PATH" / --user-data-dir PATH
        """
        if not cmd or "--user-data-dir" not in cmd.lower():
            return []
        import re

        out: List[str] = []
        patterns = (
            r'--user-data-dir=(?:"([^"]+)"|\'([^\']+)\'|([^\s"\']+))',
            r'--user-data-dir\s+(?:"([^"]+)"|\'([^\']+)\'|([^\s"\']+))',
        )
        for pat in patterns:
            for m in re.finditer(pat, cmd, flags=re.IGNORECASE):
                val = next((g for g in m.groups() if g), "")
                if val:
                    out.append(val)
        return out

    def _cmdline_references_profile(self, cmd: str, abs_profile_dir: str) -> bool:
        """
        判断进程命令行是否指向「同一」user-data-dir。

        必须用路径相等比较，不能用子串包含：
        chrome_user_data 会误匹配 chrome_user_data_rakuten /
        chrome_user_data_rakuten 会误匹配 chrome_user_data_rakuten_books。
        """
        if not cmd or not abs_profile_dir:
            return False
        target = self._normalize_profile_path(abs_profile_dir)
        if not target:
            return False
        extracted = self._extract_user_data_dirs_from_cmdline(cmd)
        if extracted:
            for raw in extracted:
                if self._normalize_profile_path(raw) == target:
                    return True
            return False
        # 回退：仅当无法解析 flag 时，要求「完整路径边界」匹配，避免前缀误伤
        c = cmd.lower().replace("/", "\\")
        needle = target.lower()
        idx = 0
        while True:
            pos = c.find(needle, idx)
            if pos < 0:
                return False
            end = pos + len(needle)
            # 后继若是路径续写字符（如 _rakuten），则不算同一目录
            if end < len(c):
                nxt = c[end]
                if nxt not in ('\\', '/', '"', "'", " ", "\t"):
                    idx = end
                    continue
            return True

    def _detect_same_profile_processes(self, user_data_dir: str) -> List[str]:
        """
        在 Windows 上检查是否已有进程命令行包含同一 --user-data-dir。
        返回命中的进程简要列表。
        """
        # 仅采集 chrome/chromedriver/msedge，避免扫描全部进程过慢
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match 'chrome|chromedriver|msedge|msedgedriver' } | "
            "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                timeout=8,
                **_subprocess_run_text_kw(),
            )
            raw = (proc.stdout or "").strip()
            if not raw:
                return []
            import json

            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            hits: List[str] = []
            for it in items:
                cmd = str((it or {}).get("CommandLine") or "")
                if not cmd:
                    continue
                if "--user-data-dir" not in cmd.lower():
                    continue
                if not self._cmdline_references_profile(cmd, user_data_dir):
                    continue
                hits.append(
                    f'PID={it.get("ProcessId")} Name={it.get("Name")}'
                )
            return hits
        except Exception:
            return []

    def _find_pids_using_profile_dir(self, user_data_dir: str) -> List[Tuple[int, str]]:
        """
        查找命令行中包含给定 user-data-dir 的 chrome.exe / chromedriver.exe 进程。
        用于在无头/多实例场景下精准结束「本项目」残留进程。
        """
        if not user_data_dir:
            return []
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match '^(?i)(chrome|chromedriver)\\.exe$' } | "
            "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                timeout=12,
                **_subprocess_run_text_kw(),
            )
            raw = (proc.stdout or "").strip()
            if not raw:
                return []
            import json

            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            out: List[Tuple[int, str]] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                cmd = str(it.get("CommandLine") or "")
                if not self._cmdline_references_profile(cmd, user_data_dir):
                    continue
                try:
                    pid = int(it.get("ProcessId") or 0)
                except Exception:
                    continue
                if pid <= 0:
                    continue
                name = str(it.get("Name") or "")
                out.append((pid, name))
            return out
        except Exception:
            return []

    def _kill_orphan_profile_processes(self, user_data_dir: str) -> List[int]:
        """结束仍占用本 profile 的 chrome/chromedriver 进程（WebDriver 已退出后的残留）。"""
        killed: List[int] = []
        for pid, _name in self._find_pids_using_profile_dir(user_data_dir):
            try:
                r = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=15,
                    **_subprocess_run_text_kw(),
                )
                if r.returncode == 0:
                    killed.append(pid)
            except Exception:
                continue
        return killed

    def close_project_browser(self, *, kill_orphans: bool = True) -> Tuple[bool, str]:
        """
        关闭本站点通过 Selenium 打开的 Chrome（driver.quit），并可选择结束同 user-data-dir 的残留进程。
        不影响其他自动化任务使用的 Chrome（除非误配同一 user_data_dir）。
        """
        log = self._logger()
        user_dir_obj = BrowserManager.get_user_data_dir_path(self.merged_config)
        user_dir_str = str(user_dir_obj) if user_dir_obj else ""

        if self.browser_manager and self.browser_manager.is_running():
            try:
                self.browser_manager.stop()
                log.info("已关闭本站点 WebDriver 浏览器会话")
            except Exception as e:
                log.error("WebDriver 关闭浏览器失败: %s", e)
                return False, str(e)

        if kill_orphans and user_dir_str:
            killed = self._kill_orphan_profile_processes(user_dir_str)
            if killed:
                log.info(
                    "已结束占用本站点 profile 的残留进程 PID: %s（user_data_dir=%s）",
                    killed,
                    user_dir_str,
                )
            else:
                log.debug("未发现需清理的同 profile 残留 chrome/chromedriver 进程")

        return True, ""

    def preflight_check(self) -> Tuple[bool, List[str]]:
        """
        启动前检查：user_data_dir 配置、锁文件、同 profile 进程占用。
        返回 (ok, messages)。ok=False 时建议阻止启动。
        """
        if self.browser_manager and self.browser_manager.is_running():
            return True, []
        messages: List[str] = []
        user_dir_obj = BrowserManager.get_user_data_dir_path(self.merged_config)
        if not user_dir_obj:
            messages.append("未配置 browser.user_data_dir，无法做 profile 占用自检")
            return False, messages

        try:
            user_dir_obj.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, [f"user_data_dir 目录不可用: {user_dir_obj} ({e})"]

        lock_info = BrowserManager.detect_profile_lock_markers(user_dir_obj)
        if lock_info.get("occupied"):
            messages.append(
                "检测到 profile 锁文件，占用风险较高: %s"
                % ", ".join(lock_info.get("markers") or [])
            )

        same_profile_pids = self._detect_same_profile_processes(str(user_dir_obj))
        if same_profile_pids:
            messages.append(
                "检测到已有进程使用同一 user_data_dir: %s"
                % "; ".join(same_profile_pids)
            )

        return len(messages) == 0, messages

    def set_ui_log(self, fn: Optional[Callable[[str], None]]) -> None:
        self.ui_log = fn

    # def pause(self) -> None: 取消掉暂停调度任务
    #     self._paused = True
    #
    # def resume(self) -> None:
    #     self._paused = False
    #
    # def is_paused(self) -> bool:
    #     return self._paused

    def _emit_ui(self, msg: str) -> None:
        if not self.ui_log:
            return
        try:
            self.root.after(0, lambda m=msg: self.ui_log(m))
        except Exception:
            pass

    def _logger(self) -> logging.Logger:
        if self.legacy_single:
            return logging.getLogger("OrderProcessor")
        return logging.getLogger(f"site.{self.site_id}")

    def _ensure_site_logger(self) -> None:
        if not self.legacy_single:
            attach_site_file_logger(self.site_id, self.merged_config)

    def _config_headless(self) -> bool:
        return bool((self.merged_config.get("browser") or {}).get("headless", False))

    def _ensure_browser_mode(
        self, *, force_headed: bool = False, reason: str = ""
    ) -> None:
        """
        确保浏览器以期望模式运行。
        - force_headed=True：强制有头（忽略配置 headless），用于骏河屋 PayPay 扫码 / 手动登录
        - force_headed=False：全自动，跟随 sites/browser.headless 配置
        若当前实例模式不符，会关闭并按同 user_data_dir 重建。
        """
        want_headless = False if force_headed else self._config_headless()
        with self._browser_lock:
            if self.browser_manager and self.browser_manager.is_running():
                if self.browser_manager.is_headless() == want_headless:
                    return
                why = reason or (
                    "PayPay 扫码强制有头" if force_headed else "恢复全自动配置"
                )
                self._logger().info(
                    "切换浏览器模式: headless %s → %s（%s）",
                    self.browser_manager.is_headless(),
                    want_headless,
                    why,
                )
                try:
                    self.browser_manager.stop()
                except Exception as e:
                    self._logger().warning("关闭旧浏览器实例失败: %s", e)
                self.browser_manager = None

            self._ensure_site_logger()
            cfg = copy.deepcopy(self.merged_config)
            cfg.setdefault("browser", {})["headless"] = want_headless
            self.browser_manager = BrowserManager(cfg)
            self.browser_manager.start()
            why = reason or ("强制有头" if force_headed else "按配置")
            self._logger().info(
                "浏览器已启动: headless=%s（%s）",
                want_headless,
                why,
            )

    def _new_order_processor(self) -> OrderProcessor:
        if not self.browser_manager or not self.browser_manager.is_running():
            raise RuntimeError("浏览器未就绪，无法创建 OrderProcessor")
        return OrderProcessor(self.merged_config, self.browser_manager)

    def start_browser_background(
        self,
        on_ready: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """在后台线程启动 Chrome，并在同线程启动调度（不依赖 GUI after，避免界面卡顿时永远不拉单）。"""

        def work():
            try:
                self._ensure_site_logger()
                # 启动系统时按配置（全自动模式）；仅骏河屋 PayPay 队列处理时再强制有头
                self._ensure_browser_mode(
                    force_headed=False, reason="全自动启动"
                )
                # 调度器必须在后台线程直接启动；仅把 UI 回调丢回主线程
                self.start_scheduler()
                self.root.after(0, on_ready)
            except Exception as e:
                # 勿写 lambda: on_error(e)：Python 3.11+ 在 except 结束后会清除 e，延迟回调会 NameError
                self.root.after(0, lambda err=e: on_error(err))

        threading.Thread(target=work, daemon=True).start()

    def start_scheduler(self) -> None:
        """假定 browser_manager 已就绪。启动定时调度。"""
        if self.scheduler is not None:
            self._logger().warning("调度器已在运行，忽略重复启动")
            return

        def run_batch() -> bool:
            if not self._begin_auto_batch():
                return True
            try:
                # 每轮新建 Fetcher/Processor，避免浏览器模式切换后仍引用旧 BrowserManager
                order_fetcher = OrderFetcher(self.merged_config)
                return self._run_order_batch(order_fetcher)
            finally:
                self._end_batch()

        sched_cfg = self.merged_config.get("scheduler", {})
        interval = int(sched_cfg.get("interval_minutes", 15) or 15)
        delay = int(sched_cfg.get("start_delay_seconds", 5) or 5)
        self.scheduler = TaskScheduler(
            interval_minutes=interval,
            start_delay_seconds=delay,
            config=self.merged_config,
        )
        self.scheduler.set_task(run_batch)
        self.scheduler.start()
        self.is_running = True
        self._logger().info(
            "已启动定时拉单：每 %s 分钟一轮（启动延迟 %s 秒）；无单时也会按该间隔继续轮询",
            interval,
            delay,
        )

    def _run_order_batch(self, order_fetcher: OrderFetcher) -> bool:
        summaries = []
        success_count = 0

        # 骏河屋：扫码时段内可先消费 PayPay 队列（强制有头）。乐天等站点完全跳过，避免误切浏览器。
        if self.adapter == "surugaya":
            pay_cfg = self.merged_config.get("payment") or {}
            auto_paypay = bool(pay_cfg.get("paypay_queue_auto_process", True))
            if auto_paypay:
                try:
                    from src.payment.payment_handler import PaymentHandler
                    from src.utils.paypay_queue import (
                        consume_all_paypay_orders,
                        get_paypay_queue_size,
                    )

                    in_window = PaymentHandler(
                        self.browser_manager, self.merged_config
                    )._is_in_paypay_scan_window()
                    queue_size = get_paypay_queue_size(self.merged_config) if in_window else 0
                    if in_window and queue_size > 0:
                        self._ensure_browser_mode(
                            force_headed=True, reason="骏河屋 PayPay 扫码"
                        )
                        order_processor = self._new_order_processor()
                        queued_orders, _ = consume_all_paypay_orders(
                            self.merged_config
                        )
                        self._logger().info(
                            "PayPay 队列：取出 %s 单（有头浏览器）", len(queued_orders)
                        )
                        for qo in queued_orders:
                            try:
                                ok, summary = order_processor.process_order(qo)
                                summaries.append(summary)
                                if ok:
                                    success_count += 1
                            except Exception as e:
                                self._logger().error(
                                    "PayPay 队列订单异常: %s", e, exc_info=True
                                )
                                order_no = str(
                                    qo.get("order_no") or qo.get("order_id") or "未知"
                                )
                                summaries.append(
                                    {
                                        "order_no": order_no,
                                        "success": False,
                                        "payment_method": "",
                                        "failure_reason": "处理异常: %s" % e,
                                        "check_cart_requested": False,
                                        "check_cart_response": "未请求",
                                        "add_no_requested": False,
                                        "add_no_response": "未请求",
                                        "update_errors": [],
                                    }
                                )
                        # 队列处理完后恢复全自动 headless 配置
                        self._ensure_browser_mode(
                            force_headed=False, reason="骏河屋恢复全自动"
                        )
                except Exception as q_err:
                    self._logger().warning("PayPay 队列处理失败（忽略）: %s", q_err)
                    try:
                        self._ensure_browser_mode(
                            force_headed=False, reason="骏河屋 PayPay 异常后恢复"
                        )
                    except Exception:
                        pass

        # 非骏河屋：不要调用 force_headed / 模式切换
        if not self.browser_manager or not self.browser_manager.is_running():
            try:
                self._ensure_browser_mode(
                    force_headed=False, reason="批处理前确保浏览器"
                )
            except Exception as e:
                self._logger().error("浏览器未就绪: %s", e, exc_info=True)
                if summaries:
                    self._flush_success_log(summaries)
                return False

        order_processor = self._new_order_processor()

        try:
            orders = order_fetcher.fetch_orders()
        except Exception as e:
            self._logger().error("拉单失败: %s", e, exc_info=True)
            if summaries:
                self._flush_success_log(summaries)
            return False

        if not orders:
            if summaries:
                self._flush_success_log(summaries)
                self._logger().info("本轮仅处理 PayPay 队列")
            else:
                self._logger().info("没有符合条件的订单")
            return True

        self._logger().info("获取到 %s 个订单", len(orders))
        for order in orders:
            try:
                ok, summary = order_processor.process_order(order)
                summaries.append(summary)
                if ok:
                    success_count += 1
            except Exception as e:
                self._logger().error("处理订单异常: %s", e, exc_info=True)
                order_no = str(order.get("order_no") or order.get("order_id") or "未知")
                summaries.append(
                    {
                        "order_no": order_no,
                        "success": False,
                        "payment_method": "",
                        "failure_reason": "处理异常: %s" % e,
                        "check_cart_requested": False,
                        "check_cart_response": "未请求",
                        "add_no_requested": False,
                        "add_no_response": "未请求",
                        "update_errors": [],
                    }
                )
            finally:
                self._sleep_order_cooldown_if_needed()

        self._flush_success_log(summaries)
        self._logger().info("任务完成，成功 %s/%s", success_count, len(orders))
        return True

    def _flush_success_log(self, summaries: list) -> None:
        if not summaries:
            return
        try:
            from src.utils.is_success_logger import write_is_success_log

            write_is_success_log(self.merged_config, summaries)
        except Exception as log_err:
            self._logger().warning("写入 is_success.log 失败: %s", log_err)

    def stop(self) -> None:
        self.is_running = False
        # self._paused = False
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
        if self.browser_manager:
            if self._keep_browser_open_on_stop():
                self._logger().info("按配置保留浏览器会话：停止系统时不关闭浏览器")
            else:
                try:
                    self.browser_manager.stop()
                except Exception:
                    pass
                self.browser_manager = None
        if not self.legacy_single:
            detach_site_handlers(self.site_id)

    def manual_login(self) -> None:
        if not self.manual_login_url:
            self._logger().warning("未配置 manual_login_url")
            return
        try:
            # 登录必须可见窗口
            if self.is_running and self.browser_manager is not None and (
                not self.browser_manager.is_running()
            ):
                raise RuntimeError(
                    "调度运行中但浏览器会话不可用；请先停止系统，关闭本站 Chrome 后重新启动"
                )
            self._ensure_browser_mode(
                force_headed=True, reason="手动登录需可见窗口"
            )
            self.browser_manager.get_driver().get(self.manual_login_url)
        except Exception as e:
            self._logger().error("打开登录页失败: %s", e)
            raise
        self._logger().info("已打开登录页: %s", self.manual_login_url)

    def process_paypay_manual(self) -> None:
        """
        仅骏河屋：手动消费 PayPay 队列（强制有头浏览器，便于人工扫码）。
        可与调度并行存在：若自动批处理正在跑，本方法立即拒绝；
        若本方法正在跑，到点的自动任务会等待本批结束后再执行。
        """
        if self.adapter != "surugaya":
            return

        def run():
            if not self._begin_manual_batch():
                return
            need_stop_browser = False
            try:
                was_running = bool(
                    self.browser_manager and self.browser_manager.is_running()
                )
                # 半自动扫码：强制有头
                self._ensure_browser_mode(
                    force_headed=True, reason="骏河屋 PayPay 手动扫码"
                )
                if not was_running:
                    need_stop_browser = True
                from src.utils.paypay_queue import consume_all_paypay_orders
                from src.utils.is_success_logger import write_is_success_log

                order_processor = self._new_order_processor()
                queued_orders, _ = consume_all_paypay_orders(self.merged_config)
                if not queued_orders:
                    self._logger().info("PayPay 队列为空")
                    return
                self._logger().info(
                    "手动 PayPay 队列：%s 单（有头浏览器）", len(queued_orders)
                )
                summaries = []
                for qo in queued_orders:
                    try:
                        ok, summary = order_processor.process_order(qo)
                        summaries.append(summary)
                    except Exception as e:
                        self._logger().error("队列订单异常: %s", e, exc_info=True)
                summaries and write_is_success_log(self.merged_config, summaries)
            finally:
                self._end_batch()
                # 若调度仍在跑，恢复全自动配置的 headless；否则按 keep_browser 决定是否关
                if self.is_running:
                    try:
                        self._ensure_browser_mode(
                            force_headed=False, reason="骏河屋手动扫码后恢复全自动"
                        )
                    except Exception as e:
                        self._logger().warning("手动队列结束后恢复浏览器模式失败: %s", e)
                elif need_stop_browser and self.browser_manager and (
                    not self._keep_browser_open_on_stop()
                ):
                    try:
                        self.browser_manager.stop()
                    except Exception:
                        pass
                    self.browser_manager = None

        threading.Thread(target=run, daemon=True).start()
