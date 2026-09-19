# -*- coding: utf-8 -*-
"""乐天统一登录适配器（市场 / 书店共用）。

触发点相同：任意店铺结算、跨站（市场↔书店）都可能落到
  login.account.rakuten.com / session/upgrade / sso/authorize …

策略偏「宽」：
  - 只认登录域名，绝不扫业务页 DOM
  - 按页面阶段信号推进：账号页 → 密码页 →（可选）2FA → 离开登录域
  - 找控件用多选择器 + 文案 + JS；DOM 已有但不 is_displayed 时仍可填
  - SPA（#/sign_in/password）给足渲染时间，避免误报「找不到 #password_current」
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


# 登录阶段（适配器内部状态机）
STAGE_NOT_LOGIN = "not_login"
STAGE_USERNAME = "username"
STAGE_PASSWORD = "password"
STAGE_TWO_FACTOR = "two_factor"
STAGE_PROFILE = "profile"
STAGE_LOGIN_UNKNOWN = "login_unknown"


class RakutenSessionGuard(LoggerMixin):
    """乐天登录适配器：市场/书店/多店铺结算共用同一套宽泛逻辑。"""

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
        'input[aria-label="パスワード"]',
        'input[aria-label*="パスワード"]',
        'input[aria-label*="Password"]',
    )
    USER_SELECTORS = (
        'input[type="email"]',
        'input[name="username"]',
        'input[name="user_id"]',
        'input[autocomplete="username"]',
        'input[aria-label*="楽天ID"]',
        'input[aria-label*="ユーザ"]',
        'input[aria-label*="メール"]',
    )
    CTA_SELECTORS = (
        "#cta",
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
        "続く",
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
    PASSWORD_HASH_HINTS = (
        "sign_in/password",
        "signin/password",
        "/password",
    )
    UPGRADE_HASH_HINTS = (
        "session_upgrade",
        "session/upgrade",
    )
    USERNAME_HASH_HINTS = (
        "sign_in/username",
        "signin/username",
        "sign_in/userid",
        "/userid",
        "/username",
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
        # Elm widget + r10-challenger 自身 watchdog 到 15/30s；默认 12s 会在表单注入前放弃
        self.form_ready_seconds = float(login_cfg.get("form_ready_seconds") or 28)
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

    def _url_bits(self, driver) -> Tuple[str, str, str]:
        try:
            url = (driver.current_url or "").strip()
        except Exception:
            return "", "", ""
        low = url.lower()
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            path = (parsed.path or "").lower()
            frag = (parsed.fragment or "").lower()
        except Exception:
            host, path, frag = "", "", ""
        # 部分 SPA 把路由放在 hash
        if not frag and "#" in low:
            try:
                frag = low.split("#", 1)[1]
            except Exception:
                frag = ""
        return host, path, frag

    def is_login_page(self, driver=None) -> bool:
        """只认登录域名。非 login.* 一律 False，绝不扫业务页 DOM。"""
        driver = driver or self.browser_manager.get_driver()
        host, path, _frag = self._url_bits(driver)
        if not host:
            return False
        if not any(h in host for h in self.LOGIN_HOST_HINTS):
            return False

        if any(p in path for p in self.LOGIN_PATH_HINTS):
            return True
        if "/login" in path or "/sso/" in path or "/session/" in path:
            return True
        return True  # 仍在登录域，保守当作登录页

    def detect_stage(self, driver=None) -> str:
        """
        宽泛识别当前登录阶段（市场 upgrade / 书店跨站 / 普通 SSO 共用）。
        优先 URL hash + 控件存在性，不要求控件一定 is_displayed。
        """
        driver = driver or self.browser_manager.get_driver()
        if not self._url_is_login_host(driver):
            return STAGE_NOT_LOGIN

        if self._page_is_additional_profile(driver):
            return STAGE_PROFILE

        _host, _path, frag = self._url_bits(driver)
        if any(h in frag for h in self.PASSWORD_HASH_HINTS):
            # hash 已是密码步（Elm 可能尚未把 #password_current 注入 DOM）
            if self._page_has_two_factor_text(driver) and not self._password_present(
                driver
            ):
                return STAGE_TWO_FACTOR
            return STAGE_PASSWORD
        if any(h in frag for h in self.UPGRADE_HASH_HINTS) and self._password_present(
            driver
        ):
            return STAGE_PASSWORD
        if any(h in frag for h in self.USERNAME_HASH_HINTS):
            return STAGE_USERNAME

        if self._page_has_two_factor_text(driver) and not self._password_present(driver):
            return STAGE_TWO_FACTOR

        if self._password_present(driver):
            return STAGE_PASSWORD
        if self._username_present(driver):
            return STAGE_USERNAME
        return STAGE_LOGIN_UNKNOWN

    def looks_like_two_factor(self, driver=None) -> bool:
        return self.detect_stage(driver) == STAGE_TWO_FACTOR

    def _page_has_two_factor_text(self, driver) -> bool:
        try:
            html = driver.page_source or ""
        except Exception:
            return False
        return any(h in html for h in self.TWO_FACTOR_HINTS)

    def _page_is_additional_profile(self, driver) -> bool:
        """楽天ビック SSO：追加の会員情報（性别必填）。"""
        try:
            html = driver.page_source or ""
        except Exception:
            return False
        if "single-option-gender-F" in html:
            return True
        return "追加の会員情報" in html and "性別" in html

    def _reset_frame(self, driver) -> None:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    def _iframe_count(self, driver) -> int:
        try:
            with self._no_implicit_wait(driver):
                return len(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
        except Exception:
            return -1

    _JS_DEEP_QUERY = """
        var sels = arguments[0];
        function walk(root, sel) {
          if (!root) return null;
          try {
            var el = root.querySelector(sel);
            if (el) return el;
          } catch (e) {}
          var nodes;
          try { nodes = root.querySelectorAll('*'); } catch (e) { return null; }
          for (var i = 0; i < nodes.length; i++) {
            var sr = nodes[i].shadowRoot;
            if (sr) {
              var f = walk(sr, sel);
              if (f) return f;
            }
          }
          return null;
        }
        for (var s = 0; s < sels.length; s++) {
          var found = walk(document, sels[s]);
          if (found) return found;
        }
        return null;
    """

    def _js_deep_find(self, driver, selectors: Tuple[str, ...]):
        try:
            return driver.execute_script(self._JS_DEEP_QUERY, list(selectors))
        except Exception:
            return None

    def _find_in_current_document(self, driver, selectors: Tuple[str, ...]):
        try:
            if "#password_current" in selectors or any(
                s == "#password_current" for s in selectors
            ):
                el = driver.execute_script(
                    "return document.getElementById('password_current');"
                )
                if el is not None:
                    return el
        except Exception:
            pass
        with self._no_implicit_wait(driver):
            el = self._first_visible(driver, selectors)
            if el is not None:
                return el
            el = self._first_present(driver, selectors)
            if el is not None:
                return el
        return self._js_deep_find(driver, selectors)

    def _find_across_frames(self, driver, selectors: Tuple[str, ...], stay: bool = False):
        """顶层 document → 嵌套 iframe。找到后 stay=True 则留在该 frame 以便填表。"""
        self._reset_frame(driver)
        el = self._find_in_current_document(driver, selectors)
        if el is not None:
            return el
        el = self._search_iframes(driver, selectors, stay=stay, depth=2, path="")
        if el is not None:
            return el
        self._reset_frame(driver)
        return None

    def _search_iframes(self, driver, selectors: Tuple[str, ...], stay: bool, depth: int, path: str):
        if depth <= 0:
            return None
        frames = []
        try:
            with self._no_implicit_wait(driver):
                frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        except Exception:
            frames = []
        for i, fr in enumerate(frames):
            try:
                driver.switch_to.frame(fr)
            except Exception:
                continue
            here = ("%s/%s" % (path, i)) if path else str(i)
            el = self._find_in_current_document(driver, selectors)
            if el is None:
                el = self._search_iframes(driver, selectors, stay, depth - 1, here)
            if el is not None:
                if stay:
                    self.logger.info("登录控件在 iframe[%s]", here)
                    return el
                self._reset_frame(driver)
                return el
            try:
                driver.switch_to.parent_frame()
            except Exception:
                self._reset_frame(driver)
                return None
        return None

    def _password_present(self, driver) -> bool:
        return self._find_across_frames(driver, self.PASSWORD_SELECTORS, stay=False) is not None

    def _username_present(self, driver) -> bool:
        return self._find_across_frames(driver, self.USER_SELECTORS, stay=False) is not None

    def ensure_logged_in(self, resume_url: Optional[str] = None) -> bool:
        """非登录页立即返回；仅 login 域才按阶段填密。"""
        if not self.enabled:
            return True

        driver = self.browser_manager.get_driver()
        self._reset_frame(driver)
        if not self.is_login_page(driver):
            return True

        stage = self.detect_stage(driver)
        self.logger.warning(
            "检测到乐天登录页，尝试自动登录（stage=%s URL=%s）",
            stage,
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

            stage = self.detect_stage(driver)
            self.logger.info(
                "乐天登录/upgrade 第 %s/%s 轮（stage=%s URL=%s）",
                upgrade_round,
                self.max_upgrade_rounds,
                stage,
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
                if upgrade_round >= self.max_upgrade_rounds:
                    raise RakutenLoginError(
                        "乐天自动登录失败（upgrade 第 %s 轮，已尝试 %s 次）: %s"
                        % (upgrade_round, self.max_login_attempts, last_err)
                    )
                self.logger.warning(
                    "乐天登录第 %s 轮未成功（%s），同一登录页继续下一轮（不刷新）",
                    upgrade_round,
                    last_err,
                )
                time.sleep(1.0)
                continue

            # 登录后若立刻又进 upgrade，继续；否则结束（不再固定空等）
            time.sleep(0.6)
            if self.is_login_page(driver):
                self.logger.warning(
                    "登录后再次出现登录页，继续下一轮（stage=%s URL=%s）",
                    self.detect_stage(driver),
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
        deadline = time.time() + max(0.2, min(float(wait_seconds), 12.0))
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

    def _widget_snapshot(self, driver) -> Dict[str, Any]:
        """对照 DevTools 拷贝：hash / Elm 壳 / 密码框是否已注入。"""
        try:
            snap = driver.execute_script(
                """
                function walkPwd(root) {
                  if (!root) return false;
                  try {
                    if (root.querySelector && root.querySelector('#password_current, input[type=password]'))
                      return true;
                  } catch (e) {}
                  var nodes;
                  try { nodes = root.querySelectorAll('*'); } catch (e) { return false; }
                  for (var i = 0; i < nodes.length; i++) {
                    if (nodes[i].shadowRoot && walkPwd(nodes[i].shadowRoot)) return true;
                  }
                  return false;
                }
                var pwd = document.getElementById('password_current');
                var ce = window.customElements;
                var hosts = [];
                try {
                  var all = document.getElementsByTagName('*');
                  for (var i = 0; i < all.length && hosts.length < 12; i++) {
                    var t = (all[i].tagName || '').toLowerCase();
                    if (t.indexOf('-') >= 0 && hosts.indexOf(t) < 0) hosts.push(t);
                  }
                } catch (e) {}
                return {
                  hash: String(location.hash || ''),
                  pwd: !!pwd,
                  shadowPwd: walkPwd(document),
                  h4k5: !!document.getElementById('h4k5-container'),
                  preview: !!document.getElementById('h4k5-preview'),
                  omniErr: !!document.getElementById('omni-error'),
                  cta: !!document.getElementById('cta011'),
                  anim: !!document.querySelector('.omni-main-view-animating'),
                  elm: (typeof Elm !== 'undefined'),
                  omni: !!(window.Rakuten && Rakuten.Omni),
                  challenger: !!(ce && ce.get && ce.get('r10-challenger')),
                  iframe: document.querySelectorAll('iframe,frame').length,
                  hosts: hosts,
                  bodyLen: document.body ? (document.body.innerHTML || '').length : -1
                };
                """
            )
            return snap if isinstance(snap, dict) else {}
        except Exception as e:
            return {"err": str(e)[:120]}

    def _wait_animating_done(self, driver) -> None:
        """`.omni-main-view-animating` 会把 password input 设成 visibility:hidden。"""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                anim = bool(
                    driver.execute_script(
                        "return !!document.querySelector('.omni-main-view-animating');"
                    )
                )
            except Exception:
                anim = False
            if not anim:
                return
            time.sleep(0.2)
        self.logger.info("登录页仍带 omni-main-view-animating，继续填密")

    @staticmethod
    def _widget_ui_missing(snap: Dict[str, Any]) -> bool:
        if snap.get("pwd") or snap.get("h4k5") or snap.get("cta") or snap.get("shadowPwd"):
            return False
        return bool(snap.get("elm") or snap.get("omni") or snap.get("challenger"))

    def _wait_form_ready(self, driver) -> str:
        """等到能取到 #password_current / 账号框（含 visibility:hidden）。同一页继续轮询，不提前刷新。"""
        deadline = time.time() + max(3.0, self.form_ready_seconds)
        last_stage = STAGE_LOGIN_UNKNOWN
        logged_shell = False
        last_miss_log = 0.0
        while time.time() < deadline:
            last_stage = self.detect_stage(driver)
            if last_stage == STAGE_PROFILE:
                return last_stage
            snap = self._widget_snapshot(driver)
            if not logged_shell and (
                snap.get("h4k5") or snap.get("pwd") or snap.get("elm") or snap.get("omni")
            ):
                self.logger.info("乐天登录 widget 快照 %s", snap)
                logged_shell = True
            if self._widget_ui_missing(snap) and (time.time() - last_miss_log) >= 8.0:
                self.logger.info(
                    "登录页脚本已加载但尚未取到密码框，继续等 snap=%s", snap
                )
                last_miss_log = time.time()
            if last_stage in (STAGE_PASSWORD, STAGE_USERNAME, STAGE_TWO_FACTOR):
                if last_stage == STAGE_PASSWORD and not self._password_present(driver):
                    time.sleep(0.3)
                    continue
                if last_stage == STAGE_USERNAME and not self._username_present(driver):
                    time.sleep(0.3)
                    continue
                return last_stage
            if self._password_present(driver):
                return STAGE_PASSWORD
            time.sleep(0.3)
        return last_stage

    def _submit_login(self, driver) -> bool:
        self._reset_frame(driver)
        self.logger.info("乐天登录 widget 初始快照 %s", self._widget_snapshot(driver))
        stage = self._wait_form_ready(driver)
        self.logger.info(
            "乐天登录适配器推进 stage=%s snap=%s",
            stage,
            self._widget_snapshot(driver),
        )

        if stage == STAGE_TWO_FACTOR:
            return False

        if stage == STAGE_PROFILE or self._page_is_additional_profile(driver):
            return self._complete_additional_profile(driver)

        if stage == STAGE_USERNAME or (
            stage == STAGE_LOGIN_UNKNOWN and self._username_present(driver)
        ):
            user_el = self._find_username_el(driver)
            if user_el is not None and self.email:
                self._fill_input(driver, user_el, self.email)
                self._click_next(driver)
                time.sleep(0.8)
                stage = self._wait_form_ready(driver)
                self.logger.info("账号页提交后 stage=%s", stage)

        password_el = self._find_password_el(driver)
        if password_el is None and stage in (
            STAGE_PASSWORD,
            STAGE_LOGIN_UNKNOWN,
            STAGE_USERNAME,
        ):
            # 再给一次短等（SPA 切到 password 路由）
            deadline = time.time() + min(12.0, max(6.0, self.form_ready_seconds / 2.0))
            while time.time() < deadline and password_el is None:
                time.sleep(0.35)
                password_el = self._find_password_el(driver)

        if password_el is None:
            snap = self._widget_snapshot(driver)
            raise RakutenLoginError(
                "登录页找不到密码输入框（#password_current；stage=%s snap=%s）"
                % (stage, snap)
            )

        # 只在当前 document 找账号框，避免再切 frame 把 password_el 弄失效
        user_el = self._find_in_current_document(driver, self.USER_SELECTORS)
        if user_el is not None and self.email:
            try:
                cur = (user_el.get_attribute("value") or "").strip()
            except Exception:
                cur = ""
            if not cur:
                self._fill_input(driver, user_el, self.email)

        self._wait_animating_done(driver)
        self._fill_input(driver, password_el, self.password)
        got = ""
        try:
            got = password_el.get_attribute("value") or ""
        except Exception:
            got = ""
        if len(got) != len(self.password):
            self.logger.warning(
                "密码框填入后长度不符 期望=%s 实际=%s，再试一次按键输入",
                len(self.password),
                len(got),
            )
            self._fill_input(driver, password_el, self.password, prefer_keys=True)
        self._wait_before_cta(driver)
        self._click_next(driver)
        self._reset_frame(driver)

        time.sleep(self.wait_after_submit_seconds)
        deadline = time.time() + self.login_timeout_seconds
        while time.time() < deadline:
            if self.looks_like_two_factor(driver):
                return False
            if self._page_is_additional_profile(driver):
                return self._complete_additional_profile(driver)
            if not self.is_login_page(driver):
                return True
            time.sleep(0.5)
        return False

    def _complete_additional_profile(self, driver) -> bool:
        """补充会员信息：选女性后点「続く」。"""
        self.logger.info("乐天登录适配器：补充会员信息，选择女性并继续")
        try:
            with self._no_implicit_wait(driver):
                gender = None
                for el in driver.find_elements(By.CSS_SELECTOR, "#single-option-gender-F"):
                    gender = el
                    break
            if gender is not None:
                driver.execute_script("arguments[0].click();", gender)
            else:
                driver.execute_script(
                    "var el=document.getElementById('single-option-gender-F');"
                    "if(el){el.click();}"
                )
        except Exception as e:
            self.logger.warning("乐天登录：点击女性选项失败: %s", e)
        time.sleep(0.4)
        try:
            self._click_next(driver)
        except Exception:
            try:
                driver.execute_script(
                    "var el=document.getElementById('cta'); if(el){el.click();}"
                )
            except Exception:
                pass
        time.sleep(self.wait_after_submit_seconds)
        deadline = time.time() + self.login_timeout_seconds
        while time.time() < deadline:
            if not self.is_login_page(driver):
                return True
            if not self._page_is_additional_profile(driver):
                stage = self.detect_stage(driver)
                if stage in (STAGE_PASSWORD, STAGE_USERNAME, STAGE_TWO_FACTOR):
                    return True
                if stage == STAGE_NOT_LOGIN:
                    return True
            time.sleep(0.4)
        return not self._page_is_additional_profile(driver)

    def _find_password_el(self, driver):
        """可见优先；iframe / open shadow DOM 一并搜。找到后留在该 frame。"""
        el = self._find_across_frames(driver, self.PASSWORD_SELECTORS, stay=True)
        if el is not None:
            try:
                shown = bool(el.is_displayed())
            except Exception:
                shown = False
            if not shown:
                self.logger.info(
                    "密码框存在但未判定为 displayed，仍尝试填入（id=%s）",
                    (el.get_attribute("id") or "")[:40],
                )
            return el
        return None

    def _find_username_el(self, driver):
        return self._find_across_frames(driver, self.USER_SELECTORS, stay=True)

    def _click_next(self, driver) -> None:
        with self._no_implicit_wait(driver):
            btn = self._find_next_button(driver)
        if btn is None:
            btn = self._js_deep_find(driver, self.CTA_SELECTORS)
        if btn is None:
            with self._no_implicit_wait(driver):
                pwd = self._find_password_el(driver)
            if pwd is not None:
                self.logger.info("未找到 Next 按钮，尝试在密码框回车")
                try:
                    pwd.send_keys(Keys.ENTER)
                except Exception:
                    # 不可交互时用 JS 触发表单/按钮
                    self._js_click_cta(driver)
                return
            if self._js_click_cta(driver):
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
            driver.execute_script(self._JS_CLICK_HUMAN, btn)
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception:
                try:
                    btn.click()
                except Exception:
                    self._js_click_cta(driver)

    def _wait_before_cta(self, driver) -> None:
        """密码页常带 r10-challenger；填完立刻点次へ会被吃掉，页面看起来像没自动登录。"""
        has_ch = False
        try:
            has_ch = bool(
                driver.execute_script(
                    """
                    function walk(root, sel) {
                      try { if (root.querySelector(sel)) return true; } catch (e) {}
                      var nodes; try { nodes = root.querySelectorAll('*'); } catch (e) { return false; }
                      for (var i = 0; i < nodes.length; i++) {
                        if (nodes[i].shadowRoot && walk(nodes[i].shadowRoot, sel)) return true;
                      }
                      return false;
                    }
                    return walk(document, 'r10-challenger, omni-11-1-ja-jp');
                    """
                )
            )
        except Exception:
            has_ch = False
        wait = 2.0 if has_ch else 0.5
        if has_ch:
            self.logger.info("登录页有 r10-challenger，填密后先等 %.1f 秒再点次へ", wait)
        time.sleep(wait)

    def _js_click_cta(self, driver) -> bool:
        try:
            ok = driver.execute_script(
                """
                function fire(el) {
                  if (!el) return false;
                  el.scrollIntoView({block:'center'});
                  var o = {bubbles:true, cancelable:true, view:window, buttons:1};
                  try { el.dispatchEvent(new PointerEvent('pointerdown', o)); } catch (e) {}
                  try { el.dispatchEvent(new MouseEvent('mousedown', o)); } catch (e) {}
                  try { el.dispatchEvent(new PointerEvent('pointerup', o)); } catch (e) {}
                  try { el.dispatchEvent(new MouseEvent('mouseup', o)); } catch (e) {}
                  try { el.dispatchEvent(new MouseEvent('click', o)); } catch (e) {}
                  try { el.click(); } catch (e) {}
                  return true;
                }
                function walk(root, fn) {
                  if (!root) return false;
                  if (fn(root)) return true;
                  var nodes; try { nodes = root.querySelectorAll('*'); } catch (e) { return false; }
                  for (var i = 0; i < nodes.length; i++) {
                    if (nodes[i].shadowRoot && walk(nodes[i].shadowRoot, fn)) return true;
                  }
                  return false;
                }
                var ids = ['cta','cta011','cta01'];
                for (var i=0;i<ids.length;i++) {
                  if (walk(document, function(root) {
                    try {
                      var el = root.getElementById ? root.getElementById(ids[i]) : root.querySelector('#'+ids[i]);
                      return fire(el);
                    } catch (e) { return false; }
                  })) return true;
                }
                return walk(document, function(root) {
                  var btns;
                  try { btns = root.querySelectorAll('.h4k5-e2e-button__submit, [role=\"button\"]'); }
                  catch (e) { return false; }
                  for (var j=0;j<btns.length;j++) {
                    var t = (btns[j].innerText || btns[j].textContent || '').trim();
                    if (t.indexOf('次へ') >= 0 || t === 'Next' || t.indexOf('ログイン') >= 0
                        || t.indexOf('続く') >= 0 || t.indexOf('続行') >= 0) {
                      return fire(btns[j]);
                    }
                  }
                  return false;
                });
                """
            )
            if ok:
                self.logger.info("JS 兜底点击登录 CTA 成功")
            return bool(ok)
        except Exception:
            return False

    def _find_next_button(self, driver):
        for css in self.CTA_SELECTORS:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, css):
                    if not self._usable(el):
                        continue
                    eid = (el.get_attribute("id") or "").lower()
                    if eid.startswith("textl_"):
                        continue
                    if eid.startswith("cta") or "h4k5-e2e-button__submit" in (
                        el.get_attribute("class") or ""
                    ):
                        return el
                    text = (
                        (el.text or "") + " " + (el.get_attribute("value") or "")
                    ).strip()
                    for t in self.NEXT_TEXTS:
                        if t.lower() in text.lower():
                            return el
            except Exception:
                continue

        try:
            for el in driver.find_elements(By.CSS_SELECTOR, '[role="button"]'):
                if not self._usable(el):
                    continue
                eid = (el.get_attribute("id") or "").lower()
                if eid.startswith("textl_"):
                    continue
                text = (
                    (el.text or "") + " " + (el.get_attribute("value") or "")
                ).strip()
                for t in self.NEXT_TEXTS:
                    if t.lower() in text.lower():
                        return el
        except Exception:
            pass

        try:
            for el in driver.find_elements(
                By.CSS_SELECTOR, "button, input[type='submit']"
            ):
                if not self._usable(el):
                    continue
                text = (
                    (el.text or "") + " " + (el.get_attribute("value") or "")
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

    def _first_present(self, driver, selectors: Tuple[str, ...]):
        for css in selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, css)
                if els:
                    return els[0]
            except Exception:
                continue
        return None

    @staticmethod
    def _js_query_exists(driver, css: str) -> bool:
        try:
            return bool(
                driver.execute_script(
                    "return !!document.querySelector(arguments[0]);", css
                )
            )
        except Exception:
            return False

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

    _JS_CLICK_HUMAN = """
        var el = arguments[0];
        if (!el) return false;
        el.scrollIntoView({block:'center'});
        var o = {bubbles:true, cancelable:true, view:window, buttons:1};
        try { el.dispatchEvent(new PointerEvent('pointerdown', o)); } catch (e) {}
        try { el.dispatchEvent(new MouseEvent('mousedown', o)); } catch (e) {}
        try { el.dispatchEvent(new PointerEvent('pointerup', o)); } catch (e) {}
        try { el.dispatchEvent(new MouseEvent('mouseup', o)); } catch (e) {}
        try { el.dispatchEvent(new MouseEvent('click', o)); } catch (e) {}
        try { el.click(); } catch (e) {}
        return true;
    """

    _JS_SET_INPUT = """
        const el = arguments[0], val = arguments[1];
        el.focus();
        const proto = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, 'value'
        );
        if (proto && proto.set) { proto.set.call(el, val); }
        else { el.value = val; }
        try {
          el.dispatchEvent(new InputEvent('input', {
            bubbles:true, cancelable:true, data: val, inputType:'insertFromPaste'
          }));
        } catch (e) {
          el.dispatchEvent(new Event('input', {bubbles:true}));
        }
        el.dispatchEvent(new Event('change', {bubbles:true}));
    """

    def _fill_input(self, driver, element, value: str, prefer_keys: bool = False) -> None:
        """乐天 SPA 只认按键/InputEvent；一次性赋 value 看起来填了，界面仍是空的。"""
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();",
                element,
            )
        except Exception:
            pass
        try:
            element.click()
        except Exception:
            pass
        time.sleep(0.12)
        try:
            element.send_keys(Keys.CONTROL, "a")
            element.send_keys(Keys.DELETE)
        except Exception:
            try:
                element.clear()
            except Exception:
                pass
        keyed = False
        try:
            element.send_keys(value)
            keyed = True
        except Exception as e:
            self.logger.info("密码/账号 send_keys 失败，改 JS: %s", e)
        try:
            current = element.get_attribute("value") or ""
        except Exception:
            current = ""
        if current != value:
            try:
                driver.execute_script(self._JS_SET_INPUT, element, value)
            except Exception as e:
                self.logger.warning("JS 写入输入框失败: %s", e)
                if not keyed:
                    try:
                        element.send_keys(value)
                    except Exception:
                        pass
        try:
            current = element.get_attribute("value") or ""
        except Exception:
            current = ""
        if current != value:
            self.logger.warning(
                "输入框写入后仍不匹配 期望长度=%s 实际长度=%s",
                len(value),
                len(current),
            )

    @staticmethod
    def _visible(el) -> bool:
        try:
            return bool(el.is_displayed())
        except Exception:
            return False

    @staticmethod
    def _usable(el) -> bool:
        """CTA：displayed 优先；否则 id=cta* 也接受（动画中）。"""
        try:
            if el.is_displayed():
                return True
        except Exception:
            pass
        try:
            eid = (el.get_attribute("id") or "").lower()
            if eid.startswith("cta"):
                return True
            cls = el.get_attribute("class") or ""
            if "h4k5-e2e-button__submit" in cls:
                return True
        except Exception:
            pass
        return False
