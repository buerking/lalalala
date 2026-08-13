"""
配置页面：在界面中修改主要配置项并保存到 config.yaml。
便于打包迁移到新电脑后快速设置 Chrome 路径、订单接口、飞书等。
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from typing import Dict, Any, Optional
import yaml
from pathlib import Path

from src.config.config_loader import ConfigLoader


# 登录与迁移说明（与 docs/迁移与登录方案.md 一致，供界面展示）
LOGIN_MIGRATION_HELP = """
【换电脑后如何登录生产骏河屋账号】

1. 本程序通过 Chrome 用户数据目录保存登录状态。换电脑后不要拷贝旧电脑的 data/chrome_user_data，否则可能无法使用或带来安全风险。

2. 在新电脑上：先在本配置页设置好「Chrome 路径」和「用户数据目录」，保存后点击主界面「启动系统」。

3. 程序会打开 Chrome 并访问骏河屋；请在弹出的浏览器窗口中手动登录一次生产用骏河屋账号（及 PayPay/PayPal 如需）。

4. 登录完成后，可关闭程序再重新启动；之后程序将自动使用本次登录状态，无需再次输入账号密码。

5. 注意：使用用户数据目录时，请勿在程序运行期间用同一目录再开一个 Chrome，否则可能报错。
"""


class SettingsWindow:
    """配置窗口：分页编辑主要配置并写回 config.yaml"""

    def __init__(self, parent: tk.Tk, config: Dict[str, Any], on_save_callback=None):
        self.parent = parent
        self.config = config
        self.on_save_callback = on_save_callback  # 保存后可选回调（如刷新主窗口 config）
        self.win: Optional[tk.Toplevel] = None
        self.entries = {}

    def open(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.lift()
            self.win.focus_force()
            return
        self.win = tk.Toplevel(self.parent)
        self.win.title("系统配置")
        self.win.geometry("620x520")
        self.win.transient(self.parent)
        self.entries = {}
        self._build_ui()
        self._load_into_form()

    def _build_ui(self):
        nb = ttk.Notebook(self.win)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 浏览器
        f_browser = ttk.Frame(nb, padding=10)
        nb.add(f_browser, text="浏览器")
        self._add_row(f_browser, "Chrome 路径 (chrome_path):", "browser.chrome_path", width=60)
        self._add_row(f_browser, "用户数据目录 (user_data_dir):", "browser.user_data_dir", width=50)
        ttk.Button(f_browser, text="浏览 Chrome...", command=self._browse_chrome).pack(anchor=tk.W, pady=(0, 5))
        self._add_check(f_browser, "无头模式 (headless)", "browser.headless", default=False)

        # 订单接口
        f_order = ttk.Frame(nb, padding=10)
        nb.add(f_order, text="订单接口")
        self._add_row(f_order, "订单详情接口 URL:", "order_api.get_order_detail_url", width=55)
        self._add_row(f_order, "secret:", "order_api.secret", width=50)
        self._add_row(f_order, "pc_mark:", "order_api.pc_mark", width=20)
        self._add_row(f_order, "待处理订单 ID 文件:", "order_api.pending_order_ids_file", width=40)

        # 飞书
        f_feishu = ttk.Frame(nb, padding=10)
        nb.add(f_feishu, text="飞书通知")
        self._add_row(f_feishu, "Webhook URL:", "feishu_webhook.url", width=55)
        self._add_row(
            f_feishu,
            "PayPay扫码群 Webhook URL:",
            "feishu_webhook.paypay_scan_url",
            width=55,
        )
        self._add_check(f_feishu, "启用飞书通知", "feishu_webhook.enabled", default=True)

        # 调度
        f_sched = ttk.Frame(nb, padding=10)
        nb.add(f_sched, text="调度")
        self._add_row(f_sched, "执行间隔(分钟):", "scheduler.interval_minutes", width=10)
        self._add_row(f_sched, "启动延迟(秒):", "scheduler.start_delay_seconds", width=10)
        
        # 支付（PayPay 扫码时间段等）
        f_payment = ttk.Frame(nb, padding=10)
        nb.add(f_payment, text="支付")
        ttk.Label(
            f_payment,
            text="PayPay 扫码时间段（24小时制，支持跨日，如 22:00-06:00；留空/[] 表示不限制）",
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 4))
        self._add_row(
            f_payment,
            "扫码时间段 (paypay_scan_time_ranges):",
            "payment.paypay_scan_time_ranges",
            width=55,
        )
        ttk.Label(
            f_payment,
            text="示例：09:00-12:00, 14:00-22:00",
            foreground="#666666",
        ).pack(anchor=tk.W, pady=(0, 8))
        self._add_row(
            f_payment,
            "提前通知等待(秒):",
            "payment.paypay_advance_notify_wait_seconds",
            width=10,
        )
        self._add_row(
            f_payment,
            "PayPay 队列文件:",
            "payment.paypay_queue_file",
            width=55,
        )
        self._add_check(
            f_payment,
            "扫码时段开始后自动处理 PayPay 队列（否则需手动点击）",
            "payment.paypay_queue_auto_process",
            default=True,
        )

        # 登录与迁移说明
        f_help = ttk.Frame(nb, padding=10)
        nb.add(f_help, text="登录与迁移说明")
        txt = tk.Text(f_help, wrap=tk.WORD, width=72, height=18, state=tk.NORMAL)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert(tk.END, LOGIN_MIGRATION_HELP.strip())
        txt.config(state=tk.DISABLED)

        # 底部按钮
        btn_frame = ttk.Frame(self.win)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="保存配置", command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="关闭", command=self.win.destroy).pack(side=tk.LEFT)

    def _add_row(self, parent, label: str, key: str, width: int = 40):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text=label, width=28, anchor=tk.W).pack(side=tk.LEFT, anchor=tk.N, padx=(0, 5))
        e = ttk.Entry(row, width=width)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entries[key] = ("entry", e)

    def _add_check(self, parent, label: str, key: str, default: bool = False):
        var = tk.BooleanVar(value=default)
        cb = ttk.Checkbutton(parent, text=label, variable=var)
        cb.pack(anchor=tk.W, pady=2)
        self.entries[key] = ("check", var)

    def _browse_chrome(self):
        path = filedialog.askopenfilename(
            title="选择 Chrome 可执行文件",
            filetypes=[("Chrome", "chrome.exe"), ("可执行文件", "*.exe"), ("全部", "*.*")]
        )
        if path:
            self.entries.get("browser.chrome_path", (None, None))[1].delete(0, tk.END)
            self.entries["browser.chrome_path"][1].insert(0, path)

    def _get_nested(self, d: dict, key_path: str):
        keys = key_path.split(".")
        for k in keys:
            d = (d or {}).get(k)
        return d

    def _set_nested(self, d: dict, key_path: str, value):
        keys = key_path.split(".")
        for k in keys[:-1]:
            if k not in d:
                d[k] = {}
            d = d[k]
        key = keys[-1]
        if key in ("interval_minutes", "start_delay_seconds", "paypay_advance_notify_wait_seconds"):
            try:
                value = int(value)
            except (ValueError, TypeError):
                value = 15 if "interval" in key_path else 5
        
        # 支付时间段：允许用户输入 "09:00-12:00,14:00-22:00" 或多行；也允许输入 "[]"
        if key == "paypay_scan_time_ranges":
            raw = (value or "").strip()
            if raw in ("", "[]", "null", "None"):
                value = []
            else:
                # 优先尝试按 YAML/JSON 列表解析（例如 ["09:00-12:00", "14:00-22:00"]）
                try:
                    parsed = yaml.safe_load(raw)
                    if isinstance(parsed, list):
                        value = [str(x).strip() for x in parsed if str(x).strip()]
                    else:
                        raise ValueError("not list")
                except Exception:
                    # 兜底：逗号/换行分隔
                    parts = []
                    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                        for p in line.split(","):
                            p = p.strip()
                            if p:
                                parts.append(p)
                    value = parts
        d[key] = value

    def _load_into_form(self):
        for key, v in self.entries.items():
            val = self._get_nested(self.config, key)
            if v[0] == "entry":
                v[1].delete(0, tk.END)
                if key == "payment.paypay_scan_time_ranges":
                    if isinstance(val, list):
                        v[1].insert(0, ", ".join(str(x) for x in val if str(x).strip()))
                    else:
                        v[1].insert(0, str(val) if val is not None else "")
                else:
                    v[1].insert(0, str(val) if val is not None else "")
            else:
                v[1].set(bool(val) if val is not None else False)

    def _save(self):
        config_path = getattr(ConfigLoader, "_config_path", None)
        if not config_path or not Path(config_path).exists():
            messagebox.showerror("保存失败", "未找到配置文件路径，无法保存。")
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for key, v in self.entries.items():
                if v[0] == "entry":
                    val = v[1].get().strip()
                else:
                    val = v[1].get()
                self._set_nested(data, key, val)
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            messagebox.showinfo("保存成功", "配置已写入 config.yaml。\n部分配置需重启程序后生效；保存后原文件中的注释可能丢失。")
            if self.on_save_callback:
                self.on_save_callback()
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
