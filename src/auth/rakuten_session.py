# -*- coding: utf-8 -*-
"""乐天会话守卫：按域名识别登录页，自动填写密码并继续。"""

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
        "/login",
        "/signin",
    )
    PASSWORD_SELECTORS = (
        'input[type="password"]',
        'input[name="password"]',
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
    NEXT_TEXTS = (
        "Next",
        "次へ",
        "次へ進む",
        "ログイン",
        "Sign in",
        "Sign In",
        "続行",
        "Continue",
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

    def __init__(self, browser_manager, config: Dict[str, Any]):
        self.browser_manager = browser_manager
        self.config = config or {}
        login_cfg = self.config.get("login") or {}
        self.email = str(login_cfg.get("email") or "").strip()
        self.password = str(login_cfg.get("password") or "")
        self.max_login_attempts = int(login_cfg.get("max_attempts") or 2)
        self.wait_after_submit_seconds = float(
            login_cfg.get("wait_after_submit_seconds") or 3
        )
        self.login_timeout_seconds = float(login_cfg.get("timeout_seconds") or 25)
        enabled = login_cfg.get("enabled")
        self.enabled = True if enabled is None else bool(enabled)

    @staticmethod
    def is_enabled(config: Dict[str, Any]) -> bool:
        adapter = str(((config or {}).get("_site") or {}).get("adapter") or "").strip()
        if adapter in ("rakuten", "rakuten_books"):
            return True
        # 市场转书店 handoff 时可能没有 _site.adapter，但配置里有 rakuten_ichiba / books
        if (config or {}).get("rakuten_ichiba") or (config or {}).get("rakuten_books"):
            login = (config or {}).get("login") or {}
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
            return False

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
            "password",
            "パスワード",
            "welcome",
            "sign in",
            "ログイン",
        )
        if host_hit and any(m in html for m in markers):
            return True
        return host_hit

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

    def ensure_logged_in(self, resume_url: Optional[str] = None) -> bool:
        """
        若当前是乐天登录页则自动填密码继续；本来就不是登录页则直接 True。
        失败抛 RakutenLoginError。
        """
        if not self.enabled:
            return True

        driver = self.browser_manager.get_driver()
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

        target = (resume_url or "").strip()
        last_err = ""
        for attempt in range(1, self.max_login_attempts + 1):
            try:
                ok = self._submit_login(driver)
            except Exception as e:
                ok = False
                last_err = str(e)
                self.logger.warning("乐天自动登录第 %s 次异常: %s", attempt, e)

            if ok:
                self.logger.info("乐天自动登录成功（第 %s 次）", attempt)
                if target and self._should_resume(target, driver):
                    self.logger.info("登录后回到目标页: %s", target)
                    try:
                        driver.get(target)
                    except Exception as e:
                        self.logger.warning("登录后跳回目标页异常（继续）: %s", e)
                    time.sleep(1.5)
                    if self.is_login_page(driver):
                        raise RakutenLoginError(
                            "登录看似成功，但回到目标页后再次出现登录页（会话可能仍无效）"
                        )
                return True

            if self.looks_like_two_factor(driver):
                raise RakutenLoginError("检测到二段階認証页面，需人工完成验证后重试")

            last_err = last_err or "仍停留在登录页"
            self.logger.warning("乐天自动登录第 %s 次未成功: %s", attempt, last_err)
            time.sleep(1.5)

        raise RakutenLoginError(
            "乐天自动登录失败（已尝试 %s 次）: %s"
            % (self.max_login_attempts, last_err)
        )

    def _submit_login(self, driver) -> bool:
        # 部分流程先邮箱/用户名再密码
        user_el = self._first_visible(driver, self.USER_SELECTORS)
        password_el = self._first_visible(driver, self.PASSWORD_SELECTORS)

        if user_el is not None and self.email and password_el is None:
            self._fill_input(driver, user_el, self.email)
            self._click_next(driver)
            time.sleep(1.2)
            password_el = self._first_visible(driver, self.PASSWORD_SELECTORS)

        if password_el is None:
            raise RakutenLoginError("登录页找不到密码输入框")

        if user_el is not None and self.email:
            # upgrade 页通常已显示账号，有用户框时也填一下
            try:
                cur = (user_el.get_attribute("value") or "").strip()
            except Exception:
                cur = ""
            if not cur:
                self._fill_input(driver, user_el, self.email)

        self._fill_input(driver, password_el, self.password)
        self._click_next(driver)

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
            time.sleep(1.0)
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
            raise RakutenLoginError("找不到登录页 Next/提交按钮")

        label = (btn.text or btn.get_attribute("value") or "").strip() or "submit"
        self.logger.info("点击乐天登录按钮: %s", label[:40])
        try:
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            btn.click()

    def _find_next_button(self, driver):
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

        # 红色主按钮兜底：页面上通常只有一个主 CTA
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
        try:
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                element,
                value,
            )
        except Exception:
            element.send_keys(value)
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
