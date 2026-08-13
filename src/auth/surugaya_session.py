# -*- coding: utf-8 -*-
"""骏河屋会话守卫：检测登录页、自动填写账号并点击「サインイン」。"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from src.utils.logger import LoggerMixin
from src.auth.cloudflare_guard import CloudflareChallengeError, CloudflareGuard


class SurugayaLoginError(Exception):
    """自动登录失败或需人工介入。"""


class SurugayaSessionGuard(LoggerMixin):
    """仅用于 adapter=surugaya。"""

    LOGIN_FORM_CSS = "#surugaya-mypage-login-form, #mypage-login-content"
    EMAIL_CSS = "input#edit-mail"
    PASSWORD_CSS = "input#edit-password"
    SUBMIT_CSS = "input#edit-submit"
    TWO_FACTOR_HINTS = (
        "二段階認証",
        "2段階認証",
        "認証コード",
        "ワンタイムパスワード",
    )

    def __init__(self, browser_manager, config: Dict[str, Any]):
        self.browser_manager = browser_manager
        self.config = config or {}
        login_cfg = self.config.get("login") or {}
        self.email = str(login_cfg.get("email") or "").strip()
        self.password = str(login_cfg.get("password") or "")
        self.max_login_attempts = int(login_cfg.get("max_attempts") or 2)
        self.wait_after_submit_seconds = float(login_cfg.get("wait_after_submit_seconds") or 3)
        self.login_timeout_seconds = float(login_cfg.get("timeout_seconds") or 25)
        self.cf_guard = CloudflareGuard(browser_manager, config)

    @staticmethod
    def is_enabled(config: Dict[str, Any]) -> bool:
        adapter = ((config or {}).get("_site") or {}).get("adapter") or "surugaya"
        return str(adapter).strip() == "surugaya"

    def credentials_ready(self) -> bool:
        return bool(self.email and self.password)

    def is_login_page(self, driver=None) -> bool:
        driver = driver or self.browser_manager.get_driver()
        try:
            forms = driver.find_elements(By.CSS_SELECTOR, self.LOGIN_FORM_CSS)
            if any(self._visible(el) for el in forms):
                return True
        except Exception:
            pass

        try:
            submits = driver.find_elements(By.CSS_SELECTOR, self.SUBMIT_CSS)
            for btn in submits:
                if not self._visible(btn):
                    continue
                value = (btn.get_attribute("value") or "").strip()
                if value == "サインイン":
                    return True
        except Exception:
            pass

        try:
            url = (driver.current_url or "").lower()
        except Exception:
            return False
        if "/pcmypage" in url and "callback=" in url:
            try:
                html = driver.page_source or ""
            except Exception:
                html = ""
            if "マイページにサインイン" in html or "surugaya-mypage-login-form" in html:
                return True
        return False

    def looks_like_two_factor(self, driver=None) -> bool:
        driver = driver or self.browser_manager.get_driver()
        try:
            html = driver.page_source or ""
        except Exception:
            return False
        return any(h in html for h in self.TWO_FACTOR_HINTS) and self.is_login_page(driver) is False

    def ensure_logged_in(self, resume_url: Optional[str] = None) -> bool:
        """
        若当前是登录页则自动登录；成功后跳回 resume_url（若提供）。
        本来就不是登录页时直接返回 True。
        失败抛 SurugayaLoginError。
        """
        driver = self.browser_manager.get_driver()
        # 人机验证优先于登录表单处理
        try:
            self.cf_guard.ensure_passed(
                resume_url=resume_url, context="骏河屋会话检查"
            )
        except CloudflareChallengeError as e:
            raise SurugayaLoginError(str(e)) from e

        if not self.is_login_page(driver):
            return True

        self.logger.warning(
            "检测到骏河屋登录页，尝试自动登录（当前URL=%s）",
            getattr(driver, "current_url", ""),
        )
        if not self.credentials_ready():
            raise SurugayaLoginError(
                "检测到登录页，但未配置 login.email / login.password，无法自动登录"
            )

        callback_fallback = self._callback_from_url(getattr(driver, "current_url", "") or "")
        target = (resume_url or "").strip() or callback_fallback

        last_err = ""
        for attempt in range(1, self.max_login_attempts + 1):
            try:
                self.cf_guard.ensure_passed(context="骏河屋登录提交前")
                ok = self._submit_login(driver)
            except CloudflareChallengeError as e:
                raise SurugayaLoginError(str(e)) from e
            except Exception as e:
                ok = False
                last_err = str(e)
                self.logger.warning("自动登录第 %s 次异常: %s", attempt, e)

            if ok:
                self.logger.info("骏河屋自动登录成功（第 %s 次）", attempt)
                if target:
                    self.logger.info("登录后回到目标页: %s", target)
                    driver.get(target)
                    wait = float(
                        (self.config.get("product_page") or {}).get("wait_after_load_seconds") or 2
                    )
                    time.sleep(wait)
                    if self.is_login_page(driver):
                        raise SurugayaLoginError(
                            "登录看似成功，但回到目标页后再次出现登录页（会话可能仍无效）"
                        )
                return True

            if self.looks_like_two_factor(driver):
                raise SurugayaLoginError("检测到二段階認証页面，需人工完成验证后重试")

            last_err = last_err or "仍停留在登录页"
            self.logger.warning("自动登录第 %s 次未成功: %s", attempt, last_err)
            time.sleep(1.5)

        raise SurugayaLoginError(
            "自动登录失败（已尝试 %s 次）: %s" % (self.max_login_attempts, last_err)
        )

    def _submit_login(self, driver) -> bool:
        email_el = self.browser_manager.wait_for_element(
            By.CSS_SELECTOR, self.EMAIL_CSS, timeout=8
        )
        password_el = self.browser_manager.wait_for_element(
            By.CSS_SELECTOR, self.PASSWORD_CSS, timeout=8
        )
        self._fill_input(driver, email_el, self.email)
        self._fill_input(driver, password_el, self.password)

        submit = None
        try:
            submit = self.browser_manager.wait_for_clickable(
                By.CSS_SELECTOR, self.SUBMIT_CSS, timeout=8
            )
        except Exception:
            submits = driver.find_elements(By.CSS_SELECTOR, self.SUBMIT_CSS)
            for btn in submits:
                if (btn.get_attribute("value") or "").strip() == "サインイン":
                    submit = btn
                    break
        if submit is None:
            raise SurugayaLoginError("找不到「サインイン」提交按钮")

        self.logger.info("点击「サインイン」")
        try:
            driver.execute_script("arguments[0].click();", submit)
        except Exception:
            submit.click()

        time.sleep(self.wait_after_submit_seconds)
        deadline = time.time() + self.login_timeout_seconds
        while time.time() < deadline:
            if self.looks_like_two_factor(driver):
                return False
            if not self.is_login_page(driver):
                return True
            # 登录失败时页面常仍显示表单，顺带看错误区
            try:
                err = driver.find_elements(By.CSS_SELECTOR, "#error-message, .messages--error")
                for el in err:
                    text = (el.text or "").strip()
                    if text:
                        self.logger.warning("登录页错误提示: %s", text[:200])
            except Exception:
                pass
            time.sleep(1.0)
        return False

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
        # 再补一次 send_keys，兼容部分 Drupal 校验
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

    @staticmethod
    def _callback_from_url(url: str) -> str:
        try:
            qs = parse_qs(urlparse(url).query)
            raw = (qs.get("callback") or [""])[0]
            path = unquote(raw or "").strip()
            if not path or path == "/pcmypage":
                return ""
            if path.startswith("http"):
                return path
            if path.startswith("/"):
                return "https://www.suruga-ya.jp" + path
        except Exception:
            pass
        return ""

    @staticmethod
    def product_id_from_url(url: str) -> str:
        m = re.search(r"/product/detail/([^/?#]+)", url or "", re.I)
        if not m:
            return ""
        pid = m.group(1).strip()
        # 购物车链接偶发带后缀 n
        if len(pid) > 1 and pid.endswith("n") and not pid[:-1].endswith("n"):
            return pid[:-1]
        return pid
