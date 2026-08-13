"""
主窗口 GUI：支持单站点（兼容旧版）与多站点 Notebook（每站点独立 Runner、日志、调度）。
"""

import logging
import threading
import tkinter as tk
from tkinter import messagebox, ttk, scrolledtext
from typing import Any, Callable, Dict, List, Optional, Union

from src.config.config_loader import ConfigLoader
from src.config.site_merge import is_multi_site_mode, list_site_entries
from src.gui.settings_window import SettingsWindow
from src.gui.site_runner import SiteRunner


def _exc_info_for_log(exc: Optional[BaseException]) -> Union[bool, tuple]:
    """
    在 root.after 主线程回调里记录「后台线程捕获的异常」时，不能使用 exc_info=True：
    此时主线程无活动异常，会误记成 NoneType: None。
    应传入 (type, value, traceback) 元组。
    """
    if exc is None:
        return False
    return (type(exc), exc, exc.__traceback__)


class MainWindow:
    """主窗口类"""

    def __init__(self, root: tk.Tk, config: Dict[str, Any], logger: logging.Logger):
        self.root = root
        self.config = config
        self.logger = logger
        self.multi_mode = is_multi_site_mode(config)

        self.runners: List[SiteRunner] = []
        self._site_tabs: List[Dict[str, Any]] = []

        # 兼容旧布局：单面板时的控件引用
        self.status_label: Optional[ttk.Label] = None
        self.start_button: Optional[ttk.Button] = None
        self.stop_button: Optional[ttk.Button] = None
        self.process_paypay_button: Optional[ttk.Button] = None
        self.next_time_label: Optional[ttk.Label] = None
        self.last_time_label: Optional[ttk.Label] = None
        self.log_text: Optional[scrolledtext.ScrolledText] = None
        self.progress_text: Optional[scrolledtext.ScrolledText] = None

        if self.multi_mode:
            entries = list_site_entries(config)
            self.runners = [SiteRunner(root, config, e, legacy_single=False) for e in entries]
        else:
            self.runners = [SiteRunner(root, config, legacy_single=True)]

        self._create_widgets()

        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        gui_config = config.get("gui", {})
        self.root.title(gui_config.get("window_title", "订单自动化处理系统"))
        window_width = gui_config.get("window_width", 1200)
        window_height = gui_config.get("window_height", 800)
        self.root.geometry(f"{window_width}x{window_height}")

        if gui_config.get("show_logs", True):
            self._setup_gui_log_handlers()

        self._update_status()

    def _create_widgets(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        if self.multi_mode:
            self._create_multi_site_layout()
        else:
            self._create_legacy_layout()

    def _create_legacy_layout(self) -> None:
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(1, weight=1)

        control_frame = ttk.LabelFrame(main_frame, text="控制面板", padding="10")
        control_frame.grid(row=0, column=0, rowspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(0, 10))

        status_frame = ttk.Frame(control_frame)
        status_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(status_frame, text="系统状态:").pack(anchor=tk.W)
        self.status_label = ttk.Label(status_frame, text="未启动", foreground="red")
        self.status_label.pack(anchor=tk.W)

        info_frame = ttk.LabelFrame(control_frame, text="任务信息", padding="5")
        info_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(info_frame, text="下次执行时间:").pack(anchor=tk.W)
        self.next_time_label = ttk.Label(info_frame, text="--")
        self.next_time_label.pack(anchor=tk.W)
        ttk.Label(info_frame, text="上次成功时间:").pack(anchor=tk.W, pady=(5, 0))
        self.last_time_label = ttk.Label(info_frame, text="--")
        self.last_time_label.pack(anchor=tk.W)

        button_frame = ttk.Frame(control_frame)
        button_frame.pack(fill=tk.X, pady=(10, 0))
        self.start_button = ttk.Button(
            button_frame, text="启动系统", command=self._legacy_start, width=20
        )
        self.start_button.pack(fill=tk.X, pady=(0, 5))
        self.stop_button = ttk.Button(
            button_frame,
            text="停止系统",
            command=self._legacy_stop,
            width=20,
            state=tk.DISABLED,
        )
        self.stop_button.pack(fill=tk.X)
        ttk.Button(button_frame, text="手动登录（打开站点）", command=self._legacy_manual_login, width=20).pack(
            fill=tk.X, pady=(10, 0)
        )
        ttk.Button(
            button_frame,
            text="关闭本项目浏览器",
            command=self._legacy_close_project_browser,
            width=20,
        ).pack(fill=tk.X, pady=(10, 0))
        ttk.Button(button_frame, text="配置", command=self._open_settings, width=20).pack(
            fill=tk.X, pady=(10, 0)
        )
        self.process_paypay_button = ttk.Button(
            button_frame,
            text="处理PayPay队列（手动）",
            command=self._legacy_paypay_manual,
            width=20,
            state=tk.NORMAL,
        )
        self.process_paypay_button.pack(fill=tk.X, pady=(10, 0))

        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="10")
        log_frame.grid(row=0, column=1, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, width=80, height=30, state=tk.DISABLED
        )
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        progress_frame = ttk.LabelFrame(main_frame, text="任务进度", padding="10")
        progress_frame.grid(row=1, column=1, sticky=(tk.W, tk.E, tk.N, tk.S))
        progress_frame.columnconfigure(0, weight=1)
        self.progress_text = scrolledtext.ScrolledText(
            progress_frame, wrap=tk.WORD, width=80, height=10, state=tk.DISABLED
        )
        self.progress_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        r0 = self.runners[0]

        def append_progress(msg: str) -> None:
            if not self.progress_text:
                return
            self.progress_text.config(state=tk.NORMAL)
            self.progress_text.insert(tk.END, msg + "\n")
            self.progress_text.see(tk.END)
            self.progress_text.config(state=tk.DISABLED)

        r0.set_ui_log(append_progress)

    def _create_multi_site_layout(self) -> None:
        outer = ttk.Frame(self.root, padding="8")
        outer.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 8))
        ttk.Button(top, text="全局：打开配置", command=self._open_settings).pack(side=tk.LEFT)

        nb = ttk.Notebook(outer)
        nb.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        for runner in self.runners:
            tab = ttk.Frame(nb, padding=6)
            nb.add(tab, text=runner.display_name)
            tab.columnconfigure(0, weight=1)
            tab.columnconfigure(1, weight=2)
            tab.rowconfigure(2, weight=1)

            left = ttk.LabelFrame(tab, text="控制", padding=8)
            left.grid(row=0, column=0, rowspan=1, sticky=(tk.W, tk.N), padx=(0, 8))

            st = ttk.Label(left, text="未启动", foreground="red")
            st.pack(anchor=tk.W)
            nt = ttk.Label(left, text="下次: --")
            nt.pack(anchor=tk.W)
            lt = ttk.Label(left, text="上次: --")
            lt.pack(anchor=tk.W)

            bf = ttk.Frame(left)
            bf.pack(fill=tk.X, pady=(8, 0))
            sb = ttk.Button(bf, text="启动", width=18)
            xb = ttk.Button(bf, text="停止", width=18, state=tk.DISABLED)
            sb.pack(fill=tk.X, pady=2)
            xb.pack(fill=tk.X, pady=2)
            lb = ttk.Button(bf, text="手动登录", width=18)
            lb.pack(fill=tk.X, pady=2)
            cb = ttk.Button(bf, text="关闭本站浏览器", width=18)
            cb.pack(fill=tk.X, pady=2)

            pay_btn: Optional[ttk.Button] = None
            if runner.adapter == "surugaya":
                pay_btn = ttk.Button(
                    bf, text="处理 PayPay 队列（手动）", width=18, state=tk.NORMAL
                )
                pay_btn.pack(fill=tk.X, pady=(8, 0))

            log_fr = ttk.LabelFrame(tab, text="日志", padding=6)
            log_fr.grid(row=0, column=1, rowspan=3, sticky=(tk.W, tk.E, tk.N, tk.S))
            log_fr.columnconfigure(0, weight=1)
            log_fr.rowconfigure(0, weight=1)
            log_tx = scrolledtext.ScrolledText(
                log_fr, wrap=tk.WORD, width=56, height=22, state=tk.DISABLED
            )
            log_tx.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

            prog_fr = ttk.LabelFrame(tab, text="任务进度", padding=6)
            prog_fr.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(8, 0))
            prog_fr.columnconfigure(0, weight=1)
            prog_fr.rowconfigure(0, weight=1)
            prog_tx = scrolledtext.ScrolledText(
                prog_fr, wrap=tk.WORD, width=56, height=10, state=tk.DISABLED
            )
            prog_tx.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

            def make_append_text(widget: scrolledtext.ScrolledText) -> Callable[[str], None]:
                def fn(msg: str) -> None:
                    widget.config(state=tk.NORMAL)
                    widget.insert(tk.END, msg + "\n")
                    widget.see(tk.END)
                    widget.config(state=tk.DISABLED)

                return fn

            runner.set_ui_log(make_append_text(prog_tx))

            ctx = {
                "runner": runner,
                "status_label": st,
                "next_time_label": nt,
                "last_time_label": lt,
                "start_btn": sb,
                "stop_btn": xb,
                "login_btn": lb,
                "close_browser_btn": cb,
                "paypay_btn": pay_btn,
                "log_text": log_tx,
            }
            self._site_tabs.append(ctx)

            sb.config(command=lambda c=ctx: self._site_start(c))
            xb.config(command=lambda c=ctx: self._site_stop(c))
            lb.config(command=lambda c=ctx: self._site_manual_login(c))
            cb.config(command=lambda c=ctx: self._site_close_project_browser(c))
            if pay_btn is not None:
                pay_btn.config(command=lambda c=ctx: self._site_paypay_manual(c))

        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

    def _setup_gui_log_handlers(self) -> None:
        gui_config = self.config.get("gui", {})
        auto_scroll = gui_config.get("auto_scroll_logs", True)

        class GuiLogHandler(logging.Handler):
            def __init__(self, text_widget: scrolledtext.ScrolledText, auto_scroll_enabled: bool):
                super().__init__()
                self.text_widget = text_widget
                self.auto_scroll_enabled = auto_scroll_enabled

            def emit(self, record: logging.LogRecord) -> None:
                msg = self.format(record)
                self.text_widget.config(state=tk.NORMAL)
                self.text_widget.insert(tk.END, msg + "\n")
                if self.auto_scroll_enabled:
                    self.text_widget.see(tk.END)
                self.text_widget.config(state=tk.DISABLED)

        fmt = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        if self.multi_mode:
            for tab in self._site_tabs:
                r = tab["runner"]
                h = GuiLogHandler(tab["log_text"], auto_scroll)
                h.setFormatter(fmt)
                logging.getLogger(f"site.{r.site_id}").addHandler(h)
        else:
            if self.log_text is None:
                return
            gui_handler = GuiLogHandler(self.log_text, auto_scroll)
            gui_handler.setFormatter(fmt)
            self.logger.addHandler(gui_handler)

    def _legacy_start(self) -> None:
        r = self.runners[0]
        if r.is_running:
            return
        ok, msgs = r.preflight_check()
        if not ok:
            err = "\n".join(f"- {m}" for m in msgs)
            self.logger.error("启动前自检未通过:\n%s", err)
            if self.status_label:
                self.status_label.config(text="启动前自检失败", foreground="red")
            messagebox.showerror("启动前自检失败", err)
            return
        if self.start_button:
            self.start_button.config(state=tk.DISABLED)
        if self.status_label:
            self.status_label.config(text="正在启动浏览器…", foreground="orange")

        def on_ready() -> None:
            # 调度器已在浏览器后台线程中启动；此处仅更新 UI
            if self.stop_button:
                self.stop_button.config(state=tk.NORMAL)
            if self.status_label:
                self.status_label.config(text="运行中", foreground="green")
            if self.process_paypay_button:
                self.process_paypay_button.config(state=tk.DISABLED)
            self.logger.info("系统已启动（调度器运行中）")

        def on_error(exc: Exception) -> None:
            try:
                self.logger.error(
                    "启动浏览器失败: %s",
                    exc if exc is not None else "(异常对象为空)",
                    exc_info=_exc_info_for_log(exc),
                )
            finally:
                if self.start_button:
                    self.start_button.config(state=tk.NORMAL)
                if self.status_label:
                    self.status_label.config(text="未启动", foreground="red")

        r.start_browser_background(on_ready, on_error)

    def _legacy_stop(self) -> None:
        r = self.runners[0]
        if not r.is_running:
            return
        try:
            r.stop()
            if self.start_button:
                self.start_button.config(state=tk.NORMAL)
            if self.stop_button:
                self.stop_button.config(state=tk.DISABLED)
            if self.status_label:
                self.status_label.config(text="已停止", foreground="red")
            if self.process_paypay_button:
                self.process_paypay_button.config(state=tk.NORMAL)
            self.logger.info("系统已停止")
        except Exception as e:
            self.logger.error("停止失败: %s", e, exc_info=True)

    def _legacy_manual_login(self) -> None:
        def work() -> None:
            try:
                self.runners[0].manual_login()
            except Exception as e:
                self.logger.error("手动登录失败: %s", e, exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def _legacy_close_project_browser(self) -> None:
        """仅结束本配置对应的 WebDriver Chrome / 同 user-data-dir 残留进程，不误杀其他自动化。"""
        r = self.runners[0]
        if r.is_running:
            if not messagebox.askyesno(
                "确认关闭浏览器",
                "调度任务正在运行中。\n关闭本项目浏览器后，下一轮自动任务可能失败，需重新「启动系统」。\n\n仍要关闭当前项目（本站点 user_data_dir）对应的 Chrome / chromedriver 吗？",
                icon="warning",
            ):
                return

        def work() -> None:
            try:
                ok, err = r.close_project_browser(kill_orphans=True)
                if not ok:
                    self.logger.error("关闭本项目浏览器失败: %s", err)
                else:
                    self.logger.info("已关闭本项目浏览器会话（并尝试清理同 profile 残留进程）")
            except Exception as e:
                self.logger.error("关闭本项目浏览器异常: %s", e, exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def _legacy_paypay_manual(self) -> None:
        r = self.runners[0]
        # 允许调度运行中点击：若自动批处理正占用，SiteRunner 内会拒绝并打日志
        r.process_paypay_manual()

    def _site_start(self, ctx: Dict[str, Any]) -> None:
        r: SiteRunner = ctx["runner"]
        if r.is_running:
            return
        ok, msgs = r.preflight_check()
        if not ok:
            err = "\n".join(f"- {m}" for m in msgs)
            logging.getLogger(f"site.{r.site_id}").error("启动前自检未通过:\n%s", err)
            ctx["status_label"].config(text="启动前自检失败", foreground="red")
            messagebox.showerror(f"{r.display_name} 启动前自检失败", err)
            return
        ctx["start_btn"].config(state=tk.DISABLED)
        ctx["status_label"].config(text="正在启动浏览器…", foreground="orange")

        def on_ready() -> None:
            # 调度器已在浏览器后台线程中启动；此处仅更新 UI
            ctx["stop_btn"].config(state=tk.NORMAL)
            ctx["status_label"].config(text="运行中", foreground="green")
            if ctx.get("paypay_btn"):
                ctx["paypay_btn"].config(state=tk.DISABLED)
            logging.getLogger(f"site.{r.site_id}").info(
                "系统已启动（调度器运行中）"
            )

        def on_error(exc: Exception) -> None:
            try:
                logging.getLogger(f"site.{r.site_id}").error(
                    "启动浏览器失败: %s",
                    exc if exc is not None else "(异常对象为空)",
                    exc_info=_exc_info_for_log(exc),
                )
            finally:
                ctx["start_btn"].config(state=tk.NORMAL)
                ctx["status_label"].config(text="未启动", foreground="red")

        r.start_browser_background(on_ready, on_error)

    def _site_stop(self, ctx: Dict[str, Any]) -> None:
        r: SiteRunner = ctx["runner"]
        if not r.is_running:
            return
        try:
            r.stop()
            ctx["start_btn"].config(state=tk.NORMAL)
            ctx["stop_btn"].config(state=tk.DISABLED)
            ctx["status_label"].config(text="已停止", foreground="red")
            if ctx.get("paypay_btn"):
                ctx["paypay_btn"].config(state=tk.NORMAL)
        except Exception as e:
            logging.getLogger(f"site.{r.site_id}").error("停止失败: %s", e, exc_info=True)

    def _site_manual_login(self, ctx: Dict[str, Any]) -> None:
        r: SiteRunner = ctx["runner"]

        def work() -> None:
            try:
                r.manual_login()
            except Exception as e:
                logging.getLogger(f"site.{r.site_id}").error("手动登录失败: %s", e, exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def _site_close_project_browser(self, ctx: Dict[str, Any]) -> None:
        r: SiteRunner = ctx["runner"]
        if r.is_running:
            if not messagebox.askyesno(
                "确认关闭浏览器",
                f"「{r.display_name}」调度正在运行中。\n关闭本站浏览器后，下一轮任务可能失败，需重新点「启动」。\n\n仍要关闭本站（本标签 user_data_dir）对应的 Chrome / chromedriver 吗？",
                icon="warning",
            ):
                return

        def work() -> None:
            log = logging.getLogger(f"site.{r.site_id}")
            try:
                ok, err = r.close_project_browser(kill_orphans=True)
                if not ok:
                    log.error("关闭本站浏览器失败: %s", err)
                else:
                    log.info("已关闭本站浏览器会话（并尝试清理同 profile 残留进程）")
            except Exception as e:
                log.error("关闭本站浏览器异常: %s", e, exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def _site_paypay_manual(self, ctx: Dict[str, Any]) -> None:
        r: SiteRunner = ctx["runner"]
        # 允许调度运行中点击：互斥由 SiteRunner._begin_manual_batch 保证
        r.process_paypay_manual()

    def _update_status(self) -> None:
        if self.multi_mode:
            for ctx in self._site_tabs:
                r = ctx["runner"]
                if r.scheduler and r.is_running:
                    status = r.scheduler.get_status()
                    nt = status.get("next_execution_time")
                    if nt:
                        try:
                            from datetime import datetime

                            nd = datetime.fromisoformat(nt)
                            ctx["next_time_label"].config(text="下次: " + nd.strftime("%Y-%m-%d %H:%M:%S"))
                        except Exception:
                            ctx["next_time_label"].config(text="下次: " + str(nt))
                    else:
                        ctx["next_time_label"].config(text="下次: --")
                    lst = status.get("last_success_time")
                    if lst:
                        try:
                            from datetime import datetime

                            ld = datetime.fromisoformat(lst)
                            ctx["last_time_label"].config(text="上次: " + ld.strftime("%Y-%m-%d %H:%M:%S"))
                        except Exception:
                            ctx["last_time_label"].config(text="上次: " + str(lst))
                    else:
                        ctx["last_time_label"].config(text="上次: --")
                    # if r.is_paused():
                    #     ctx["status_label"].config(text="已暂停（需人工）", foreground="orange")
                else:
                    ctx["next_time_label"].config(text="下次: --")
                    ctx["last_time_label"].config(text="上次: --")
        else:
            r = self.runners[0]
            if self.next_time_label and self.last_time_label and r.scheduler and r.is_running:
                status = r.scheduler.get_status()
                next_time = status.get("next_execution_time")
                if next_time:
                    try:
                        from datetime import datetime

                        nd = datetime.fromisoformat(next_time)
                        self.next_time_label.config(text=nd.strftime("%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        self.next_time_label.config(text=str(next_time))
                else:
                    self.next_time_label.config(text="--")
                last_time = status.get("last_success_time")
                if last_time:
                    try:
                        from datetime import datetime

                        ld = datetime.fromisoformat(last_time)
                        self.last_time_label.config(text=ld.strftime("%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        self.last_time_label.config(text=str(last_time))
                else:
                    self.last_time_label.config(text="--")

        self.root.after(5000, self._update_status)

    def _open_settings(self) -> None:
        def on_save() -> None:
            try:
                self.config = ConfigLoader.reload_config()
            except Exception:
                pass

        SettingsWindow(self.root, self.config, on_save_callback=on_save).open()

    def _on_closing(self) -> None:
        for r in self.runners:
            if r.is_running:
                try:
                    r.stop()
                except Exception:
                    pass
        self.root.destroy()


def _bootstrap_path_for_direct_run() -> None:
    """从 IDE 直接运行本文件时，把项目根目录加入 sys.path。"""
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    # main_window.py 位于 <根>/src/gui/
    root = os.path.abspath(os.path.join(here, "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)


if __name__ == "__main__":
    _bootstrap_path_for_direct_run()
    import tkinter as tk

    from src.config.config_loader import ConfigLoader
    from src.utils.logger import setup_logger

    try:
        config = ConfigLoader.load_config()
        logger = setup_logger(config)
        logger.info("通过 main_window.py 直接启动（建议日常使用项目根目录的 main.py）")
        root = tk.Tk()
        MainWindow(root, config, logger)
        root.mainloop()
    except Exception as e:
        print("程序启动失败: %s" % e)
        import traceback

        traceback.print_exc()
        raise SystemExit(1) from e
