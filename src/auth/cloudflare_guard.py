# -*- coding: utf-8 -*-
"""Cloudflare / 人机验证门卫：检测挑战页，有头模式下等待人工通过。"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

from selenium.webdriver.common.by import By

from src.utils.logger import LoggerMixin


class CloudflareChallengeError(Exception):
    """等待人工通过 Cloudflare 超时或无法继续。"""


class CloudflareGuard(LoggerMixin):
    """
    不自动破解验证码；在 headed 浏览器中检测挑战页并等待人工点击，
    通过后继续原业务流程。
    """

    TITLE_HINTS = (
        "just a moment",
        "attention required",
        "please wait",
        "checking your browser",
        "セキュリティチェック",
        "しばらくお待ち",
    )
    # 仅用于「标题已像挑战页」时的辅助计数；禁止单独用 cloudflare 字样判真
    BODY_HINTS = (
        "cdn-cgi/challenge",
        "cf-browser-verification",
        "cf-challenge-running",
        "challenge-platform",
        "checking your browser before accessing",
        "attention required! | cloudflare",
        "人による確認",
        "ブラウザを確認",
    )
    # 强特征：真实挑战页才有；不含 cloudflareinsights 分析脚本
    STRONG_MARKERS = (
        "cf-browser-verification",
        "cf-challenge-running",
        "cdn-cgi/challenge",
        "challenge-platform",
        "challenges.cloudflare.com",
        "checking your browser before accessing",
        "attention required! | cloudflare",
    )
    SELECTORS = (
        "#challenge-form",
        "#challenge-running",
        "#cf-challenge-running",
        "iframe[src*='challenges.cloudflare.com']",
        "iframe[src*='turnstile']",
        "div.cf-turnstile",
        "input[name='cf-turnstile-response']",
    )
    # 骏河屋等正常业务页特征：有这些且看不到挑战控件 → 不应当成 CF
    NORMAL_PAGE_MARKERS = (
        "現在購入予定アイテムはありません",
        "ショッピングカート",
        "data-drupal-messages",
        "suruga-ya.jp",
        'class="cart"',
        "blockCart",
        "csrf_token",
        "view_cart",
        "買い物かご",
        "商品詳細",
    )

    def __init__(self, browser_manager, config: Dict[str, Any]):
        self.browser_manager = browser_manager
        self.config = config or {}
        cf = self.config.get("cloudflare") or {}
        self.enabled = cf.get("enabled", True) is not False
        self.wait_seconds = float(cf.get("wait_seconds") or 180)
        self.poll_seconds = float(cf.get("poll_seconds") or 2.5)
        self.notify = cf.get("feishu_notify", True) is not False
        # 发飞书前再确认一次，避免页面闪一下 / insights 误报
        self.confirm_seconds = float(cf.get("confirm_seconds") or 2.0)
        self._feishu = None
        self._last_notify_at = 0.0

    @staticmethod
    def is_surugaya(config: Dict[str, Any]) -> bool:
        adapter = ((config or {}).get("_site") or {}).get("adapter") or "surugaya"
        return str(adapter).strip() == "surugaya"

    def _feishu_notifier(self):
        if self._feishu is not None:
            return self._feishu
        try:
            from src.notification.feishu_notifier import FeishuNotifier

            self._feishu = FeishuNotifier(self.config)
        except Exception:
            self._feishu = False
        return self._feishu if self._feishu is not False else None

    @staticmethod
    def _strip_analytics_noise(html: str) -> str:
        """去掉 Cloudflare Insights 等分析脚本，避免把 cloudflare 字样当挑战。"""
        if not html:
            return ""
        html = re.sub(
            r"<script[^>]+cloudflareinsights\.com[^>]*>.*?</script>",
            " ",
            html,
            flags=re.I | re.S,
        )
        html = re.sub(
            r"https?://static\.cloudflareinsights\.com[^\s\"']*",
            " ",
            html,
            flags=re.I,
        )
        html = re.sub(r"data-cf-beacon\s*=\s*'[^']*'", " ", html, flags=re.I)
        html = re.sub(r'data-cf-beacon\s*=\s*"[^"]*"', " ", html, flags=re.I)
        return html

    def _has_visible_challenge_widget(self, driver) -> bool:
        for sel in self.SELECTORS:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if any(self._visible(el) for el in els):
                    return True
            except Exception:
                continue
        return False

    def _looks_like_normal_site_page(self, html: str, title: str) -> bool:
        blob = ((title or "") + "\n" + (html or "")).lower()
        raw = (title or "") + "\n" + (html or "")
        hits = 0
        for m in self.NORMAL_PAGE_MARKERS:
            if not m:
                continue
            if m.lower() in blob or m in raw:
                hits += 1
            if hits >= 2:
                return True
        return False

    def is_challenge_page(self, driver=None) -> bool:
        driver = driver or self.browser_manager.get_driver()
        try:
            title = (driver.title or "").strip().lower()
        except Exception:
            title = ""
        try:
            cur_url = (driver.current_url or "").lower()
        except Exception:
            cur_url = ""
        # Cloudflare 挑战过程中常给目标 URL 临时挂上 __cf_chl_rt_tk / __cf_chl_*。
        # 若业务页内容已出现且无可见挑战控件，视为过程参数残留，不再当成挑战中
        # （否则会空等接近 wait_seconds，表现为「开购物车很久才开始加购」）。
        if "__cf_chl_" in cur_url or "__cf_chl_rt_tk=" in cur_url:
            visible_widget = self._has_visible_challenge_widget(driver)
            try:
                raw_html = driver.page_source or ""
            except Exception:
                raw_html = ""
            if visible_widget:
                return True
            if self._looks_like_normal_site_page(raw_html, title):
                return False
            return True

        visible_widget = self._has_visible_challenge_widget(driver)

        try:
            raw_html = driver.page_source or ""
        except Exception:
            raw_html = ""
        html = self._strip_analytics_noise(raw_html).lower()

        # 正常业务页 + 无可见挑战控件 → 直接否（购物车空页也会带 insights beacon）
        if self._looks_like_normal_site_page(raw_html, title) and not visible_widget:
            return False

        if visible_widget:
            return True

        if any(h in title for h in self.TITLE_HINTS):
            # 标题像挑战页时，还要有正文强特征，避免空标题/短暂闪烁
            if any(m in html for m in self.STRONG_MARKERS) or "turnstile" in html:
                return True
            soft_hits = sum(1 for h in self.BODY_HINTS if h in html)
            if soft_hits >= 1:
                return True
            return False

        if any(m in html for m in self.STRONG_MARKERS):
            return True
        if "cf-turnstile" in html and "challenges.cloudflare.com" in html:
            return True
        return False

    def ensure_passed(
        self,
        *,
        resume_url: Optional[str] = None,
        context: str = "",
    ) -> bool:
        """
        若当前为人机页：飞书提醒并轮询等待人工通过。
        通过后可选回到 resume_url。未检测到则直接返回 True。
        """
        if not self.enabled:
            return True
        driver = self.browser_manager.get_driver()
        if not self.is_challenge_page(driver):
            return True

        # 二次确认，避免导航瞬间误报 / 自动通过后的残留
        time.sleep(max(0.5, self.confirm_seconds))
        if not self.is_challenge_page(driver):
            self.logger.info(
                "Cloudflare 初检疑似挑战，二次确认已通过（可能是页面闪烁或 insights 误报）%s",
                (" context=" + context) if context else "",
            )
            return True

        url = ""
        try:
            url = driver.current_url or ""
        except Exception:
            pass
        self.logger.warning(
            "检测到 Cloudflare/人机验证页，等待人工在浏览器窗口通过（最多 %.0f 秒）%s URL=%s",
            self.wait_seconds,
            (" context=" + context) if context else "",
            url,
        )
        self._notify_once(url=url, context=context)

        deadline = time.time() + max(15.0, self.wait_seconds)
        while time.time() < deadline:
            time.sleep(max(0.8, self.poll_seconds))
            if not self.is_challenge_page(driver):
                self.logger.info("Cloudflare/人机验证已通过，继续流程")
                # 通过后若已在目标商品页（去掉 __cf_chl_* 后路径一致），禁止再 driver.get，
                # 否则极易再次挂上 __cf_chl_rt_tk 进入挑战死循环。
                target = (resume_url or "").strip()
                if target:
                    try:
                        cur = driver.current_url or ""
                    except Exception:
                        cur = ""
                    if self._same_business_url(cur, target):
                        self.logger.info(
                            "验证通过后已在目标页，跳过再次导航: %s", cur
                        )
                    else:
                        self.logger.info("验证通过后回到目标页: %s", target)
                        driver.get(target)
                        time.sleep(1.5)
                        if self.is_challenge_page(driver):
                            continue
                return True
        raise CloudflareChallengeError(
            "等待 Cloudflare/人机验证超时（%.0f 秒），请人工通过后重试。URL=%s"
            % (self.wait_seconds, url)
        )

    @staticmethod
    def _same_business_url(a: str, b: str) -> bool:
        """比较业务 URL：忽略 hash、__cf_chl_* 等 Cloudflare 过程参数。"""
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        def norm(u: str) -> str:
            u = (u or "").strip()
            if not u:
                return ""
            p = urlparse(u)
            q = [
                (k, v)
                for k, v in parse_qsl(p.query, keep_blank_values=True)
                if not str(k).lower().startswith("__cf_")
            ]
            q.sort(key=lambda kv: kv[0])
            return urlunparse(
                (
                    (p.scheme or "").lower(),
                    (p.netloc or "").lower(),
                    (p.path or "").rstrip("/") or "/",
                    "",
                    urlencode(q),
                    "",
                )
            )

        return norm(a) == norm(b) and bool(norm(a))

    def _notify_once(self, *, url: str, context: str) -> None:
        if not self.notify:
            return
        now = time.time()
        if now - self._last_notify_at < 60:
            return
        self._last_notify_at = now
        notifier = self._feishu_notifier()
        if not notifier:
            return
        try:
            site = ((self.config.get("_site") or {}).get("display_name") or "骏河屋")
            notifier.send_message(
                "Cloudflare 人机验证待处理",
                (
                    f"**站点**: {site}\n"
                    f"**说明**: 浏览器窗口出现 Cloudflare/人机验证，请到自动化 Chrome 窗口手动完成勾选/验证。\n"
                    f"**上下文**: {context or '页面导航'}\n"
                    f"**URL**: {url or '(未知)'}\n"
                    f"**等待上限**: {int(self.wait_seconds)} 秒"
                ),
            )
        except Exception as e:
            self.logger.warning("飞书提醒 Cloudflare 失败: %s", e)

    @staticmethod
    def _visible(el) -> bool:
        try:
            return bool(el.is_displayed())
        except Exception:
            return False
