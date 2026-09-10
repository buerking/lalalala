"""
浏览器管理器
"""

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from typing import Dict, Any, Optional, Union
import logging
import os
from pathlib import Path

from src.utils.logger import LoggerMixin
from src.utils.retry import retry


class BrowserManager(LoggerMixin):
    """浏览器管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化浏览器管理器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.browser_config = config.get('browser', {})
        self.driver: Optional[WebDriver] = None
        self._headless_active: bool = bool(self.browser_config.get("headless", False))

    def _engine(self) -> str:
        raw = str(self.browser_config.get("type") or self.browser_config.get("engine") or "chrome")
        kind = raw.strip().lower()
        if kind in ("edge", "msedge", "microsoft-edge"):
            return "edge"
        return "chrome"

    def _default_edge_binary(self) -> str:
        candidates = [
            str(self.browser_config.get("edge_path") or "").strip(),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.join(
                os.environ.get("LOCALAPPDATA") or "",
                r"Microsoft\Edge\Application\msedge.exe",
            ),
        ]
        for p in candidates:
            if p and os.path.isfile(p):
                return p
        return ""

    def _project_root(self) -> Path:
        return Path(__file__).parent.parent.parent

    def _get_driver_binary_path(self) -> Optional[Path]:
        """固定驱动：Chrome 用 chromedriver_path，Edge 用 edgedriver_path。"""
        if self._engine() == "edge":
            raw = str(
                self.browser_config.get("edgedriver_path")
                or os.environ.get("EDGEDRIVER")
                or os.environ.get("MSEDGEDRIVER")
                or ""
            ).strip()
            key = "edgedriver_path"
            default_name = "msedgedriver.exe"
        else:
            raw = str(
                self.browser_config.get("chromedriver_path")
                or os.environ.get("CHROMEDRIVER")
                or ""
            ).strip()
            key = "chromedriver_path"
            default_name = "chromedriver.exe"
        if not raw:
            fallback = self._project_root() / "tools" / default_name
            if fallback.is_file():
                return fallback.resolve()
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = (self._project_root() / path).resolve()
        if not path.is_file():
            raise FileNotFoundError("%s 路径不存在: %s" % (key, path))
        return path

    def is_headless(self) -> bool:
        """当前实例是否以无头模式启动（启动后不变，直至 stop/重建）。"""
        return bool(self._headless_active)
    @staticmethod
    def get_user_data_dir_path(config: Dict[str, Any]) -> Optional[Path]:
        """从配置解析 user_data_dir 绝对路径。"""
        browser_cfg = (config or {}).get("browser") or {}
        user_data_dir = str(browser_cfg.get("user_data_dir") or "").strip()
        if not user_data_dir:
            return None
        if not os.path.isabs(user_data_dir):
            project_root = Path(__file__).parent.parent.parent
            return (project_root / user_data_dir).resolve()
        return Path(user_data_dir).resolve()

    def _get_chromedriver_path(self) -> Optional[Path]:
        """兼容旧调用；Edge 时读 edgedriver_path。"""
        return self._get_driver_binary_path()

    @staticmethod
    def detect_profile_lock_markers(user_data_dir: Optional[Path]) -> Dict[str, Any]:
        """
        检查 Chrome profile 常见锁文件。
        返回: {"occupied": bool, "markers": [path, ...]}
        """
        if not user_data_dir:
            return {"occupied": False, "markers": []}
        marker_names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
        markers = []
        for n in marker_names:
            p = user_data_dir / n
            if p.exists():
                markers.append(str(p))
        return {"occupied": bool(markers), "markers": markers}

    @staticmethod
    def navigate_allow_timeout(
        driver: webdriver.Chrome,
        url: str,
        logger: Optional[Union[logging.Logger, Any]] = None,
    ) -> bool:
        """
        导航到 URL。若触发 page_load_timeout，则 window.stop() 后继续使用已渲染的 DOM。
        返回 True 表示在超时前完成加载，False 表示超时后已 stop 并继续。
        """
        try:
            driver.get(url)
            return True
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            if logger is not None:
                try:
                    logger.warning("页面加载超时，已停止加载并继续操作: %s", url)
                except Exception:
                    pass
            return False
    
    def start(self):
        """启动浏览器"""
        if self.driver is not None:
            self.logger.warning("浏览器已经在运行中")
            return

        engine = self._engine()
        try:
            if engine == "edge":
                binary = self._default_edge_binary()
                if not binary:
                    raise FileNotFoundError(
                        "未找到 Microsoft Edge。请安装 Edge，或在 config 配置 browser.edge_path"
                    )
                options: Union[ChromeOptions, EdgeOptions] = EdgeOptions()
            else:
                binary = str(self.browser_config.get("chrome_path") or "").strip()
                if not binary or not os.path.exists(binary):
                    raise FileNotFoundError("Chrome浏览器路径不存在: %s" % binary)
                options = ChromeOptions()
            options.binary_location = binary

            if self.browser_config.get("headless", False):
                options.add_argument("--headless")
                if self.browser_config.get("headless_disable_gpu", True):
                    options.add_argument("--disable-gpu")
                    options.add_argument("--disable-software-rasterizer")
                self._headless_active = True
            else:
                self._headless_active = False

            for raw in self.browser_config.get("extra_chrome_args") or []:
                s = str(raw).strip()
                if s:
                    options.add_argument(s)

            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--lang=ja-JP")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
            options.add_experimental_option(
                "prefs",
                {
                    "intl.accept_languages": str(
                        self.browser_config.get("accept_languages")
                        or "ja-JP,ja,en-US,en"
                    ),
                },
            )

            pls = str(self.browser_config.get("page_load_strategy") or "").strip().lower()
            if pls in ("normal", "eager", "none"):
                options.page_load_strategy = pls
                self.logger.info("%s page_load_strategy=%s", engine, pls)

            window_width = self.browser_config.get("window_width", 1920)
            window_height = self.browser_config.get("window_height", 1080)
            options.add_argument("--window-size=%s,%s" % (window_width, window_height))

            user_data_dir_obj = self.get_user_data_dir_path(self.config)
            if user_data_dir_obj:
                user_data_dir_obj.mkdir(parents=True, exist_ok=True)
                lock_info = self.detect_profile_lock_markers(user_data_dir_obj)
                if lock_info.get("occupied"):
                    raise RuntimeError(
                        "检测到 user_data_dir 正在被占用（发现锁文件），请先关闭占用该 profile 的浏览器/自动化：%s"
                        % ", ".join(lock_info.get("markers") or [])
                    )
                user_data_dir = str(user_data_dir_obj)
                options.add_argument("--user-data-dir=%s" % user_data_dir)
                self.logger.info("使用用户数据目录保持登录状态: %s", user_data_dir)
            else:
                self.logger.warning("未配置用户数据目录，登录状态将不会保持")
                self.logger.warning("建议在config.yaml中配置 browser.user_data_dir")

            driver_path = self._get_driver_binary_path()
            self.logger.info("启动浏览器 engine=%s binary=%s", engine, binary)
            if engine == "edge":
                if driver_path:
                    self.logger.info("使用固定 msedgedriver: %s", driver_path)
                    self.driver = webdriver.Edge(
                        service=EdgeService(executable_path=str(driver_path)),
                        options=options,
                    )
                else:
                    self.logger.warning(
                        "未配置 browser.edgedriver_path 且 tools/msedgedriver.exe 不存在，"
                        "将使用 Selenium Manager 解析 msedgedriver（国内网络常失败，建议本机放置驱动）"
                    )
                    self.driver = webdriver.Edge(options=options)
            else:
                if driver_path:
                    self.logger.info("使用固定 chromedriver: %s", driver_path)
                    self.driver = webdriver.Chrome(
                        service=ChromeService(executable_path=str(driver_path)),
                        options=options,
                    )
                else:
                    self.logger.warning(
                        "未配置 browser.chromedriver_path，将使用 Selenium Manager，首次启动可能较慢"
                    )
                    self.driver = webdriver.Chrome(options=options)

            implicit_wait = self.browser_config.get("implicit_wait", 10)
            self.driver.implicitly_wait(implicit_wait)

            page_load_timeout = self.browser_config.get("page_load_timeout", 30)
            self.driver.set_page_load_timeout(page_load_timeout)

            self._apply_stealth_patches()
            self.driver.get("about:blank")
            self.logger.info("浏览器启动成功（%s）", engine)

        except Exception as e:
            self.logger.error("浏览器启动失败: %s", e)
            if "user data directory is already in use" in str(e).lower():
                self.logger.error("用户数据目录正在被使用，请关闭占用同一 profile 的浏览器实例")
            raise

    def _apply_stealth_patches(self) -> None:
        """通过 CDP 弱化常见自动化特征；失败不影响启动。"""
        if self.driver is None:
            return
        if self.browser_config.get("stealth", True) is False:
            return
        lang = str(self.browser_config.get("accept_languages") or "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7")
        try:
            self.driver.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {
                    "userAgent": self.driver.execute_script("return navigator.userAgent;"),
                    "acceptLanguage": lang,
                    "platform": "Win32",
                },
            )
        except Exception as e:
            self.logger.debug("CDP UserAgentOverride 跳过: %s", e)
        script = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
Object.defineProperty(navigator, 'language', {get: () => 'ja-JP'});
try {
  window.chrome = window.chrome || { runtime: {} };
} catch (e) {}
"""
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": script},
            )
            self.logger.info("已启用浏览器 stealth 补丁（隐藏 webdriver / 日语语言）")
        except Exception as e:
            self.logger.warning("stealth 补丁未生效: %s", e)

        # 日语界面与时区，贴近日本站真实访问
        try:
            tz = str(self.browser_config.get("timezone_id") or "Asia/Tokyo")
            self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": tz})
        except Exception:
            pass
        try:
            self.driver.execute_cdp_cmd(
                "Emulation.setLocaleOverride", {"locale": "ja-JP"}
            )
        except Exception:
            pass

    def stop(self):
        """停止浏览器"""
        if self.driver is not None:
            try:
                self.driver.quit()
                self.logger.info("浏览器已关闭")
            except Exception as e:
                self.logger.error(f"关闭浏览器时出错: {e}")
            finally:
                self.driver = None
    
    def get_driver(self) -> WebDriver:
        """
        获取WebDriver实例
        
        Returns:
            WebDriver实例
            
        Raises:
            RuntimeError: 浏览器未启动
        """
        if self.driver is None:
            raise RuntimeError("浏览器未启动，请先调用start()方法")
        return self.driver

    def ensure_alive(self, *, restart_if_dead: bool = True) -> webdriver.Chrome:
        """
        确保当前有可用浏览器窗口。
        常见问题：用户手动关掉了 Chrome 窗口，但 chromedriver 会话仍在，
        后续操作会立刻报 no such window / web view not found。
        """
        driver = self.get_driver()
        try:
            handles = list(driver.window_handles or [])
        except Exception as e:
            self.logger.warning("浏览器会话已失效（无法读取窗口）: %s", e)
            handles = []

        if handles:
            try:
                cur = driver.current_window_handle
                if cur in handles:
                    # 探活：访问一下 URL
                    _ = driver.current_url
                    return driver
            except Exception:
                pass
            try:
                driver.switch_to.window(handles[-1])
                _ = driver.current_url
                self.logger.info(
                    "已切换到剩余浏览器窗口（原窗口已关闭）handles=%s",
                    len(handles),
                )
                return driver
            except Exception as e:
                self.logger.warning("切换剩余窗口失败: %s", e)

        if not restart_if_dead:
            raise RuntimeError(
                "浏览器窗口已关闭（no such window），请重新启动系统或手动打开浏览器会话"
            )

        self.logger.warning("浏览器窗口/会话不可用，尝试重新启动浏览器…")
        try:
            self.stop()
        except Exception:
            self.driver = None
        self.start()
        return self.get_driver()
    
    def is_running(self) -> bool:
        """检查浏览器是否在运行"""
        return self.driver is not None

    def find_elements_now(self, by: By, value: str):
        """
        探测可选元素时禁用隐式等待，避免选择器不存在时每个都干等 implicit_wait 秒。
        """
        driver = self.get_driver()
        implicit_wait = float(self.browser_config.get("implicit_wait", 10))
        try:
            driver.implicitly_wait(0)
            return driver.find_elements(by, value)
        finally:
            driver.implicitly_wait(implicit_wait)

    def navigate(self, url: str) -> bool:
        """见 navigate_allow_timeout。"""
        return self.navigate_allow_timeout(self.get_driver(), url, self.logger)
    
    def wait_for_element(self, by: By, value: str, timeout: int = 10):
        """
        等待元素出现
        
        Args:
            by: 定位方式
            value: 定位值
            timeout: 超时时间（秒）
            
        Returns:
            WebElement实例
        """
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(EC.presence_of_element_located((by, value)))
    
    def wait_for_clickable(self, by: By, value: str, timeout: int = 10):
        """
        等待元素可点击
        
        Args:
            by: 定位方式
            value: 定位值
            timeout: 超时时间（秒）
            
        Returns:
            WebElement实例
        """
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(EC.element_to_be_clickable((by, value)))

