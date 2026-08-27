# -*- coding: utf-8 -*-
"""乐天会话守卫：仅在登录域名出现时自动填密。

兼容 session/upgrade（#password_current + div#cta011）。
重要：非登录页禁止扫 DOM；find_elements 必须 implicit_wait=0，
否则默认 10s 隐式等待会把清空购物车/加购拖成数分钟。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from src.utils.logger import LoggerMixin


class RakutenLoginError(Exception):
    """自动登录失败或需人工介入。"""


class RakutenSessionGuard(LoggerMixin):
    LOGIN_HOST_HINTS = (
        "login.account.rakuten.com",
        "login.rakuten.co.jp",
        "member.id.rakuten.co.jp",
        "glogin.rakuten.co.jp",
        "id.rakuten.co.jp",
    )
    # 仅精确登录路径；不要加 /widget 等会误伤业务页的宽泛片段
    LOGIN_PATH_HINTS = (
        "/session/upgrade",
        "/sso/authorize",
        "/sso/login",
        "/signin",
    )
    PASSWORD_SELECTORS = (
        "#password_current",
        'input[name="password"]',
        'input[type="password"]',
        'input[autocomplete="current-password"]',
    )
    USER_SELECTORS = (
        'input[type="email"]',
        'input[name="username"]',
        'input[name="user_id"]',
        'input[autocomplete="username"]',
    )
    CTA_SELECTORS = (
        "#cta011",
        "#cta01",
        ".h4k5-e2e-button__submit",
        '[role="button"].h4k5-e2e-button__submit',
        'button[type="submit"]',
        'input[type="submit"]',
    )
    NEXT_TEXTS = (
        "次へ",
        "Next",
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
        login_cfg = self._resolve_login_cfg(self.config)
        self.email = str(login_cfg.get("email") or "").strip()
        self.password = str(login_cfg.get("password") or "")
        self.max_login_attempts = int(login_cfg.get("max_attempts") or 2)
        self.max_upgrade_rounds = int(login_cfg.get("max_upgrade_rounds") or 3)
        self.wait_after_submit_seconds = float(
            login_cfg.get("wait_after_submit_seconds") or 2
        )
        self.login_timeout_seconds = float(login_cfg.get("timeout_seconds") or 20)
        enabled = login_cfg.get("enabled")
        self.enabled = True if enabled is None else bool(enabled)

    @staticmethod
    def _resolve_login_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
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
        if (config or {}).get("rakuten_ichiba") or (config or {}).get("rakuten_books"):
            login = RakutenSessionGuard._resolve_login_cfg(config or {})
            if login.get("password"):
                return True
        return False

    def credentials_ready(self) -> bool:
        return bool(self.password)

    @contextmanager
    def _no_implicit_wait(self, driver) -> Iterator[None]:
        """探测元素时关闭隐式等待，避免每个缺失选择器空等 10 秒。"""
        prev = 0.0
        try:
            prev = float(
                (self.config.get("browser") or {}).get("implicit_wait", 10) or 10
            )
        except Exception:
            prev = 10.0
        try:
            driver.implicitly_wait(0)
            yield
        finally:
            try:
                driver.implicitly_wait(prev)
            except Exception:
                pass

    def _url_is_login_host(self, driver=None) -> bool:
        driver = driver or self.browser_manager.get_driver()
        try:
            url = (driver.current_url or "").strip().lower()
        except Exception:
            return False
        if not url or url.startswith("data:") or url == "about:blank":
            return False
        return any(h in url for h in self.LOGIN_HOST_HINTS)

    def is_login_page(self, driver=None) -> bool:
        """
        只认登录域名。非 login.* 一律 False，绝不扫业务页 DOM。
        """
        driver = driver or self.browser_manager.get_driver()
        try:
            url = (driver.current_url or "").strip().lower()
        except Exception:
            return False
        if not url or url.startswith("data:") or url == "about:blank":
            return False

        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            path = (parsed.path or "").lower()
        except Exception:
            return False

        if not any(h in host for h in self.LOGIN_HOST_HINTS):
            return False

        # 登录域名即视为登录流程（upgrade/authorize 等）
        if any(p in path for p in self.LOGIN_PATH_HINTS):
            return True
        if "/login" in path or "/sso/" in path or "/session/" in path:
            return True

        # 其它登录域路径：有密码框才算
        with self._no_implicit_wait(driver):
            try:
                for css in self.PASSWORD_SELECTORS[:3]:
                    for el in driver.find_elements(By.CSS_SELECTOR, css):
                        if self._visible(el):
                            return True
            except Exception:
                pass
        return True  # 仍在登录域，保守当作登录页

    def looks_like_two_factor(self, driver=None) -> bool:
        driver = driver or self.browser_manager.get_driver()
        if not self._url_is_login_host(driver):
            return False
        try:
            html = driver.page_source or ""
        except Exception:
            return False
        if not any(h in html for h in self.TWO_FACTOR_HINTS):
            return False
        # 密码页不算 2FA
        with self._no_implicit_wait(driver):
            try:
                for css in self.PASSWORD_SELECTORS[:2]:
                    if any(
                        self._visible(el)
                        for el in driver.find_elements(By.CSS_SELECTOR, css)
                    ):
                        return False
            except Exception:
                pass
        return True

    def ensure_logged_in(self, resume_url: Optional[str] = None) -> bool:
        """非登录页立即返回；仅 login 域才填密。"""
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
                time.sleep(0.8)

            if not round_ok:
                raise RakutenLoginError(
                    "乐天自动登录失败（upgrade 第 %s 轮，已尝试 %s 次）: %s"
                    % (upgrade_round, self.max_login_attempts, last_err)
                )

            # 登录后若立刻又进 upgrade，继续；否则结束（不再固定空等）
            time.sleep(0.6)
            if self.is_login_page(driver):
                self.logger.warning(
                    "登录后再次出现登录页，继续下一轮（URL=%s）",
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
            time.sleep(1.0)
            if self.is_login_page(driver):
                if not self._submit_login(driver):
                    raise RakutenLoginError(
                        "登录看似成功，但回到目标页后再次出现登录页且无法通过"
                    )
                time.sleep(0.8)
            if self.is_login_page(driver):
                raise RakutenLoginError(
                    "登录看似成功，但回到目标页后再次出现登录页（会话可能仍无效）"
                )
        return True

    def ensure_after_possible_redirect(
        self, resume_url: Optional[str] = None, wait_seconds: float = 1.0
    ) -> bool:
        """
        点击后可能跳到 login 域：只按 URL 短轮询，不上业务页扫 DOM。
        """
        if not self.enabled:
            return True
        driver = self.browser_manager.get_driver()
        # 默认最多约 1 秒；已在登录域则立刻处理
        deadline = time.time() + max(0.2, min(float(wait_seconds), 2.0))
        while time.time() < deadline:
            if self._url_is_login_host(driver):
                break
            time.sleep(0.2)
        return self.ensure_logged_in(resume_url=resume_url)

    @staticmethod
    def _sanitize_resume_url(resume_url: Optional[str]) -> str:
        target = (resume_url or "").strip()
        if not target:
            return ""
        low = target.lower()
        if any(h in low for h in RakutenSessionGuard.LOGIN_HOST_HINTS):
            return ""
        return target

    def _submit_login(self, driver) -> bool:
        # 仅在已确认登录域时调用；短等密码框出现（SPA）
        password_el = None
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with self._no_implicit_wait(driver):
                password_el = self._first_visible(driver, self.PASSWORD_SELECTORS)
                user_el = self._first_visible(driver, self.USER_SELECTORS)
            if password_el is not None:
                break
            if user_el is not None and self.email:
                self._fill_input(driver, user_el, self.email)
                self._click_next(driver)
                time.sleep(0.8)
                continue
            time.sleep(0.25)

        if password_el is None:
            with self._no_implicit_wait(driver):
                password_el = self._first_visible(driver, self.PASSWORD_SELECTORS)
        if password_el is None:
            raise RakutenLoginError("登录页找不到密码输入框（#password_current）")

        with self._no_implicit_wait(driver):
            user_el = self._first_visible(driver, self.USER_SELECTORS)
        if user_el is not None and self.email:
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
            time.sleep(0.5)
        return False

    def _click_next(self, driver) -> None:
        with self._no_implicit_wait(driver):
            btn = self._find_next_button(driver)
        if btn is None:
            with self._no_implicit_wait(driver):
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
        for css in self.CTA_SELECTORS:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, css):
                    if not self._visible(el):
                        continue
                    eid = (el.get_attribute("id") or "").lower()
                    if eid.startswith("textl_"):
                        continue
                    if eid.startswith("cta") or "h4k5-e2e-button__submit" in (
                        el.get_attribute("class") or ""
                    ):
                        return el
                    text = ((el.text or "") + " " + (el.get_attribute("value") or "")).strip()
                    for t in self.NEXT_TEXTS:
                        if t.lower() in text.lower():
                            return el
            except Exception:
                continue

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

        try:
            for el in driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit']"):
                if not self._visible(el):
                    continue
                text = (
                    (el.text or "")
                    + " "
                    + (el.get_attribute("value") or "")
                ).strip()
                for t in self.NEXT_TEXTS:
                    if t.lower() in text.lower():
                        return el
                if (el.get_attribute("type") or "").lower() == "submit":
                    return el
        except Exception:
            pass
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
        if any(h in cur for h in RakutenSessionGuard.LOGIN_HOST_HINTS):
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
