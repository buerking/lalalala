# -*- coding: utf-8 -*-
"""乐天会话守卫：按域名识别登录页，自动填写密码并继续。

兼容场景（市场⇄书店、购物车 SSO）：
- login.account.rakuten.com/session/upgrade（ようこそ + 仅密码）
- 密码框 #password_current / name=password
- 主按钮多为 div#cta011（role=button），不是 <button>
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from src.utils.logger import LoggerMixin


class RakutenLoginError(Exception):
    """自动登录失败或需人工介入。"""


class RakutenSessionGuard(LoggerMixin):
    """
    用于 adapter=rakuten / rakuten_books。
    主要通过当前 URL 域名判断是否落在乐天统一登录（如 login.account.rakuten.com）。
    """

    LOGIN_HOST_HINTS = (
        "login.account.rakuten.com",
        "login.rakuten.co.jp",
        "member.id.rakuten.co.jp",
        "glogin.rakuten.co.jp",
        "id.rakuten.co.jp",
    )
    LOGIN_PATH_HINTS = (
        "/session/upgrade",
        "/sso/authorize",
        "/sso/login",
        "/login",
        "/signin",
        "/widget",
    )
    PASSWORD_SELECTORS = (
        "#password_current",
        'input#password_current',
        'input[name="password"]',
        'input[type="password"]',
        'input[name="p"]',
        'input#password',
        'input[autocomplete="current-password"]',
    )
    USER_SELECTORS = (
        'input[type="email"]',
        'input[name="username"]',
        'input[name="user_id"]',
        'input[name="u"]',
        'input#user_id',
        'input[autocomplete="username"]',
    )
    # 新登录 UI：主 CTA 常是 div[role=button]，优先用稳定 id/class
    CTA_SELECTORS = (
        "#cta011",
        "#cta01",
        "#cta02",
        ".h4k5-e2e-button__submit",
        '[role="button"].h4k5-e2e-button__submit',
        '[role="button"].sbt',
        'button[type="submit"]',
        'input[type="submit"]',
        "button",
    )
    NEXT_TEXTS = (
        "Next",
        "次へ",
        "次へ進む",
        "ログイン",
        "Sign in",
        "Sign In",
        "続行",
        "Continue",
        "送信",
    )
    TWO_FACTOR_HINTS = (
        "二段階認証",
        "2段階認証",
        "認証コード",
        "ワンタイムパスワード",
        "verification code",
        "two-step",
        "2-step",
    )
    CHALLENGER_HINTS = (
        "r10-challenger",
        "omni-",
        "challenge",
    )

    def __init__(self, browser_manager, config: Dict[str, Any]):
        self.browser_manager = browser_manager
        self.config = config or {}
        login_cfg = self._resolve_login_cfg(self.config)
        self.email = str(login_cfg.get("email") or "").strip()
        self.password = str(login_cfg.get("password") or "")
        self.max_login_attempts = int(login_cfg.get("max_attempts") or 3)
        self.max_upgrade_rounds = int(login_cfg.get("max_upgrade_rounds") or 4)
        self.wait_after_submit_seconds = float(
            login_cfg.get("wait_after_submit_seconds") or 3
        )
        self.login_timeout_seconds = float(login_cfg.get("timeout_seconds") or 30)
        enabled = login_cfg.get("enabled")
        self.enabled = True if enabled is None else bool(enabled)

    @staticmethod
    def _resolve_login_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
        """顶层 login 优先；否则从站点块 / 嵌套块兜底。"""
        cfg = config or {}
        top = cfg.get("login") or {}
        if isinstance(top, dict) and (top.get("password") or top.get("email")):
            return top
        for key in ("rakuten", "rakuten_ichiba", "rakuten_books"):
            block = cfg.get(key) or {}
            if not isinstance(block, dict):
                continue
            nested = block.get("login") or {}
            if isinstance(nested, dict) and (
                nested.get("password") or nested.get("email")
            ):
                return nested
        return top if isinstance(top, dict) else {}

    @staticmethod
    def is_enabled(config: Dict[str, Any]) -> bool:
        adapter = str(((config or {}).get("_site") or {}).get("adapter") or "").strip()
        if adapter in ("rakuten", "rakuten_books"):
            return True
        # 市场转书店 handoff 时可能没有 _site.adapter，但配置里有 rakuten_ichiba / books
        if (config or {}).get("rakuten_ichiba") or (config or {}).get("rakuten_books"):
            login = RakutenSessionGuard._resolve_login_cfg(config or {})
            if login.get("password"):
                return True
        return False

    def credentials_ready(self) -> bool:
        return bool(self.password)

    def is_login_page(self, driver=None) -> bool:
        driver = driver or self.browser_manager.get_driver()
        try:
            url = (driver.current_url or "").strip().lower()
        except Exception:
            return False
        if not url or url.startswith("data:") or url == "about:blank":
            return False

        host = ""
        path = ""
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            path = (parsed.path or "").lower()
        except Exception:
            host = url

        host_hit = any(h in host for h in self.LOGIN_HOST_HINTS)
        path_hit = any(p in path for p in self.LOGIN_PATH_HINTS)
        if not host_hit and not (host.endswith("rakuten.com") and path_hit):
            # 非登录域名：若出现明确 upgrade UI 仍识别（iframe 极少见）
            if not self._has_upgrade_ui(driver):
                return False

        # 明确 UI：#password_current / #cta011 / ようこそ
        if self._has_upgrade_ui(driver):
            return True

        # 域名已命中登录站：再确认有密码框或典型文案，减少误判
        try:
            for css in self.PASSWORD_SELECTORS:
                for el in driver.find_elements(By.CSS_SELECTOR, css):
                    if self._visible(el):
                        return True
        except Exception:
            pass

        try:
            html = (driver.page_source or "").lower()
        except Exception:
            html = ""
        markers = (
            "password (required)",
            "password_current",
            "password",
            "パスワード",
            "ようこそ",
            "welcome",
            "sign in",
            "ログイン",
            "session/upgrade",
            "cta011",
        )
        if host_hit and any(m in html for m in markers):
            return True
        return bool(host_hit and path_hit)

    def _has_upgrade_ui(self, driver) -> bool:
        try:
            for css in ("#password_current", 'input[name="password"]', "#cta011"):
                for el in driver.find_elements(By.CSS_SELECTOR, css):
                    if self._visible(el):
                        return True
        except Exception:
            pass
        try:
            html = driver.page_source or ""
        except Exception:
            return False
        if "password_current" in html and ("cta011" in html or "次へ" in html):
            return True
        if "ようこそ" in html and "パスワード" in html and "次へ" in html:
            # 避免业务页误伤：再要求登录域名或 password input
            try:
                url = (driver.current_url or "").lower()
            except Exception:
                url = ""
            if any(h in url for h in self.LOGIN_HOST_HINTS):
                return True
        return False

    def looks_like_two_factor(self, driver=None) -> bool:
        driver = driver or self.browser_manager.get_driver()
        try:
            html = driver.page_source or ""
        except Exception:
            return False
        if any(h in html for h in self.TWO_FACTOR_HINTS):
            # 仍是密码登录页时不算 2FA
            if self.is_login_page(driver):
                try:
                    for css in self.PASSWORD_SELECTORS:
                        if any(
                            self._visible(el)
                            for el in driver.find_elements(By.CSS_SELECTOR, css)
                        ):
                            return False
                except Exception:
                    pass
            return True
        return False

    def looks_like_challenger(self, driver=None) -> bool:
        """登录页上的 r10-challenger / 人机组件。"""
        driver = driver or self.browser_manager.get_driver()
        try:
            for css in ("r10-challenger", "omni-11-1-ja-jp", "[c-refresh-sec]"):
                els = driver.find_elements(By.CSS_SELECTOR, css)
                if els:
                    return True
        except Exception:
            pass
        try:
            html = (driver.page_source or "").lower()
        except Exception:
            return False
        return any(h in html for h in self.CHALLENGER_HINTS) and self.is_login_page(
            driver
        )

    def ensure_logged_in(self, resume_url: Optional[str] = None) -> bool:
        """
        若当前是乐天登录页则自动填密码继续；本来就不是登录页则直接 True。
        市场⇄书店 / 购物车会反复出现 session/upgrade，支持连续多轮。
        失败抛 RakutenLoginError。
        """
        if not self.enabled:
            return True

        driver = self.browser_manager.get_driver()
        # SPA 可能稍后才渲染密码框：短等
        if not self.is_login_page(driver):
            self._wait_for_login_ui(driver, timeout=1.2)
        if not self.is_login_page(driver):
            return True

        self.logger.warning(
            "检测到乐天登录页，尝试自动登录（当前URL=%s）",
            getattr(driver, "current_url", ""),
        )
        if not self.credentials_ready():
            raise RakutenLoginError(
                "检测到乐天登录页，但未配置 login.password，无法自动登录"
            )

        target = self._sanitize_resume_url(resume_url)
        last_err = ""
        for upgrade_round in range(1, self.max_upgrade_rounds + 1):
            if not self.is_login_page(driver):
                break

            self.logger.info(
                "乐天登录/upgrade 第 %s/%s 轮（URL=%s）",
                upgrade_round,
                self.max_upgrade_rounds,
                getattr(driver, "current_url", ""),
            )
            round_ok = False
            for attempt in range(1, self.max_login_attempts + 1):
                try:
                    ok = self._submit_login(driver)
                except Exception as e:
                    ok = False
                    last_err = str(e)
                    self.logger.warning(
                        "乐天自动登录第 %s 轮第 %s 次异常: %s",
                        upgrade_round,
                        attempt,
                        e,
                    )

                if ok:
                    self.logger.info(
                        "乐天自动登录成功（第 %s 轮第 %s 次）",
                        upgrade_round,
                        attempt,
                    )
                    round_ok = True
                    break

                if self.looks_like_two_factor(driver):
                    raise RakutenLoginError(
                        "检测到二段階認証页面，需人工完成验证后重试"
                    )

                last_err = last_err or "仍停留在登录页"
                self.logger.warning(
                    "乐天自动登录第 %s 轮第 %s 次未成功: %s",
                    upgrade_round,
                    attempt,
                    last_err,
                )
                time.sleep(1.2)

            if not round_ok:
                raise RakutenLoginError(
                    "乐天自动登录失败（upgrade 第 %s 轮，已尝试 %s 次）: %s"
                    % (upgrade_round, self.max_login_attempts, last_err)
                )

            # 登录后可能立刻再跳另一个 upgrade（市场⇄书店）
            self._wait_for_login_ui(driver, timeout=2.0)
            if self.is_login_page(driver):
                self.logger.warning(
                    "登录后再次出现登录页，继续下一轮 upgrade（URL=%s）",
                    getattr(driver, "current_url", ""),
                )
                continue
            break

        if target and self._should_resume(target, driver):
            self.logger.info("登录后回到目标页: %s", target)
            try:
                driver.get(target)
            except Exception as e:
                self.logger.warning("登录后跳回目标页异常（继续）: %s", e)
            time.sleep(1.5)
            # 回到业务页后又可能被踢回 upgrade
            for _ in range(self.max_upgrade_rounds):
                if not self.is_login_page(driver):
                    break
                self.logger.warning(
                    "回到目标页后又出现登录页，再次自动登录（URL=%s）",
                    getattr(driver, "current_url", ""),
                )
                if not self._submit_login(driver):
                    raise RakutenLoginError(
                        "登录看似成功，但回到目标页后再次出现登录页且无法通过"
                    )
                time.sleep(1.0)
            if self.is_login_page(driver):
                raise RakutenLoginError(
                    "登录看似成功，但回到目标页后再次出现登录页（会话可能仍无效）"
                )
        return True

    def ensure_after_possible_redirect(
        self, resume_url: Optional[str] = None, wait_seconds: float = 3.0
    ) -> bool:
        """
        点击「購入手続き/次へ」等后可能异步跳到 session/upgrade。
        短轮询等待登录页出现，再 ensure。
        """
        if not self.enabled:
            return True
        driver = self.browser_manager.get_driver()
        deadline = time.time() + max(0.5, float(wait_seconds))
        while time.time() < deadline:
            if self.is_login_page(driver):
                break
            time.sleep(0.35)
        return self.ensure_logged_in(resume_url=resume_url)

    def _wait_for_login_ui(self, driver, timeout: float = 2.0) -> None:
        deadline = time.time() + max(0.2, float(timeout))
        while time.time() < deadline:
            if self.is_login_page(driver) or self._has_upgrade_ui(driver):
                return
            time.sleep(0.25)

    @staticmethod
    def _sanitize_resume_url(resume_url: Optional[str]) -> str:
        target = (resume_url or "").strip()
        if not target:
            return ""
        low = target.lower()
        bad = (
            "login.account.rakuten",
            "login.rakuten.co.jp",
            "member.id.rakuten",
            "glogin.rakuten",
            "id.rakuten.co.jp",
        )
        if any(h in low for h in bad):
            return ""
        return target

    def _submit_login(self, driver) -> bool:
        # 等 SPA 渲染
        self._wait_for_login_ui(driver, timeout=3.0)

        # 部分流程先邮箱/用户名再密码
        user_el = self._first_visible(driver, self.USER_SELECTORS)
        password_el = self._first_visible(driver, self.PASSWORD_SELECTORS)

        if user_el is not None and self.email and password_el is None:
            self._fill_input(driver, user_el, self.email)
            self._click_next(driver)
            time.sleep(1.2)
            password_el = self._first_visible(driver, self.PASSWORD_SELECTORS)

        if password_el is None:
            # 再等一轮（upgrade 页异步）
            self._wait_for_login_ui(driver, timeout=2.5)
            password_el = self._first_visible(driver, self.PASSWORD_SELECTORS)
        if password_el is None:
            raise RakutenLoginError("登录页找不到密码输入框（#password_current）")

        if user_el is not None and self.email:
            # upgrade 页通常已显示账号，有用户框时也填一下
            try:
                cur = (user_el.get_attribute("value") or "").strip()
            except Exception:
                cur = ""
            if not cur:
                self._fill_input(driver, user_el, self.email)

        self._fill_input(driver, password_el, self.password)
        time.sleep(0.25)
        self._click_next(driver)

        if self.looks_like_challenger(driver):
            self.logger.warning(
                "登录页存在 r10-challenger，等待其完成（最长 %.0fs）",
                self.login_timeout_seconds,
            )

        time.sleep(self.wait_after_submit_seconds)
        deadline = time.time() + self.login_timeout_seconds
        while time.time() < deadline:
            if self.looks_like_two_factor(driver):
                return False
            if not self.is_login_page(driver):
                return True
            try:
                err = driver.find_elements(
                    By.CSS_SELECTOR,
                    '[role="alert"], .error, .error-message, .rf-form-error',
                )
                for el in err:
                    text = (el.text or "").strip()
                    if text:
                        self.logger.warning("乐天登录页错误提示: %s", text[:200])
            except Exception:
                pass
            time.sleep(0.8)
        return False

    def _click_next(self, driver) -> None:
        btn = self._find_next_button(driver)
        if btn is None:
            # 兜底：在密码框按 Enter
            pwd = self._first_visible(driver, self.PASSWORD_SELECTORS)
            if pwd is not None:
                self.logger.info("未找到 Next 按钮，尝试在密码框回车")
                pwd.send_keys(Keys.ENTER)
                return
            raise RakutenLoginError("找不到登录页 Next/提交按钮（#cta011）")

        label = (
            (btn.text or "")
            or (btn.get_attribute("value") or "")
            or (btn.get_attribute("id") or "")
            or "submit"
        ).strip()
        self.logger.info("点击乐天登录按钮: %s", label[:40])
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});"
                "arguments[0].click();",
                btn,
            )
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception:
                btn.click()

    def _find_next_button(self, driver):
        # 1) 稳定选择器（含 div#cta011）
        for css in self.CTA_SELECTORS:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, css):
                    if not self._visible(el):
                        continue
                    # 跳过「忘记密码 / 别的 ID」等次要链接
                    eid = (el.get_attribute("id") or "").lower()
                    if eid.startswith("textl_"):
                        continue
                    text = (
                        (el.text or "")
                        + " "
                        + (el.get_attribute("value") or "")
                        + " "
                        + (el.get_attribute("aria-label") or "")
                        + " "
                        + (el.get_attribute("class") or "")
                    )
                    # 明确 CTA id / e2e class 直接用
                    if eid.startswith("cta") or "h4k5-e2e-button__submit" in (
                        el.get_attribute("class") or ""
                    ):
                        return el
                    for t in self.NEXT_TEXTS:
                        if t.lower() in text.lower():
                            return el
            except Exception:
                continue

        # 2) 任意 role=button 文案匹配
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, '[role="button"]'):
                if not self._visible(el):
                    continue
                eid = (el.get_attribute("id") or "").lower()
                if eid.startswith("textl_"):
                    continue
                text = ((el.text or "") + " " + (el.get_attribute("value") or "")).strip()
                for t in self.NEXT_TEXTS:
                    if t.lower() in text.lower():
                        return el
        except Exception:
            pass

        # 3) 传统 button / submit
        candidates = []
        try:
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, "button"))
            candidates.extend(
                driver.find_elements(By.CSS_SELECTOR, 'input[type="submit"]')
            )
            candidates.extend(
                driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]')
            )
        except Exception:
            return None

        for el in candidates:
            if not self._visible(el):
                continue
            text = (
                (el.text or "")
                + " "
                + (el.get_attribute("value") or "")
                + " "
                + (el.get_attribute("aria-label") or "")
            ).strip()
            for t in self.NEXT_TEXTS:
                if t.lower() in text.lower():
                    return el

        for el in candidates:
            if not self._visible(el):
                continue
            try:
                typ = (el.get_attribute("type") or "").lower()
            except Exception:
                typ = ""
            if typ == "submit":
                return el
        return None

    def _first_visible(self, driver, selectors: Tuple[str, ...]):
        for css in selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, css):
                    if self._visible(el):
                        return el
            except Exception:
                continue
        return None

    @staticmethod
    def _should_resume(target: str, driver) -> bool:
        try:
            cur = (driver.current_url or "").lower()
        except Exception:
            return True
        t = (target or "").lower()
        if not t:
            return False
        # 已自然跳回业务域则不必再 get
        if "login.account.rakuten" in cur or "member.id.rakuten" in cur:
            return True
        try:
            th = urlparse(t).netloc.lower()
            ch = urlparse(cur).netloc.lower()
            if th and ch and th == ch:
                return False
        except Exception:
            pass
        return True

    @staticmethod
    def _fill_input(driver, element, value: str) -> None:
        try:
            element.click()
        except Exception:
            pass
        try:
            element.clear()
        except Exception:
            try:
                element.send_keys(Keys.CONTROL, "a")
                element.send_keys(Keys.DELETE)
            except Exception:
                pass
        # React 受控输入：先 JS 设值再补键盘事件
        try:
            driver.execute_script(
                """
                const el = arguments[0], val = arguments[1];
                const proto = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                );
                if (proto && proto.set) { proto.set.call(el, val); }
                else { el.value = val; }
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """,
                element,
                value,
            )
        except Exception:
            try:
                element.send_keys(value)
            except Exception:
                pass
        try:
            current = element.get_attribute("value") or ""
            if current != value:
                element.clear()
                element.send_keys(value)
        except Exception:
            pass

    @staticmethod
    def _visible(el) -> bool:
        try:
            return bool(el.is_displayed())
        except Exception:
            return False
