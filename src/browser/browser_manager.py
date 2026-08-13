"""
浏览器管理器
"""

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
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
        self.driver: Optional[webdriver.Chrome] = None
        self._headless_active: bool = bool(self.browser_config.get("headless", False))

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
        """优先使用固定驱动路径，避免每次等待 Selenium Manager 联网解析。"""
        raw = str(
            self.browser_config.get("chromedriver_path")
            or os.environ.get("CHROMEDRIVER")
            or ""
        ).strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            project_root = Path(__file__).parent.parent.parent
            path = (project_root / path).resolve()
        if not path.is_file():
            raise FileNotFoundError("chromedriver 路径不存在: %s" % path)
        return path

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
        
        try:
            chrome_path = self.browser_config.get('chrome_path', '')
            if not chrome_path or not os.path.exists(chrome_path):
                raise FileNotFoundError(f"Chrome浏览器路径不存在: {chrome_path}")
            
            # 配置Chrome选项
            chrome_options = Options()
            chrome_options.binary_location = chrome_path
            
            if self.browser_config.get('headless', False):
                chrome_options.add_argument('--headless')
                # 无头模式在部分 Windows 环境下易因 GPU/DevTools 端口启动失败，默认缓解
                if self.browser_config.get('headless_disable_gpu', True):
                    chrome_options.add_argument('--disable-gpu')
                    chrome_options.add_argument('--disable-software-rasterizer')
                self._headless_active = True
            else:
                self._headless_active = False

            # 可选：由配置追加参数（如 --remote-debugging-port=0 等），见 config browser.extra_chrome_args
            for raw in self.browser_config.get("extra_chrome_args") or []:
                s = str(raw).strip()
                if s:
                    chrome_options.add_argument(s)

            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-blink-features=AutomationControlled')
            chrome_options.add_argument('--lang=ja-JP')
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            chrome_options.add_experimental_option(
                "prefs",
                {
                    "intl.accept_languages": str(
                        self.browser_config.get("accept_languages")
                        or "ja-JP,ja,en-US,en"
                    ),
                },
            )

            # eager：DOM 就绪即返回，不等待全部资源；none：完全不等待（需配合 navigate_allow_timeout）
            pls = str(self.browser_config.get("page_load_strategy") or "").strip().lower()
            if pls in ("normal", "eager", "none"):
                chrome_options.page_load_strategy = pls
                self.logger.info("Chrome page_load_strategy=%s", pls)
            
            # 设置窗口大小
            window_width = self.browser_config.get('window_width', 1920)
            window_height = self.browser_config.get('window_height', 1080)
            chrome_options.add_argument(f'--window-size={window_width},{window_height}')
            
            # 配置用户数据目录（用于保持登录状态）
            user_data_dir_obj = self.get_user_data_dir_path(self.config)
            if user_data_dir_obj:
                # 创建目录（如果不存在）
                user_data_dir_obj.mkdir(parents=True, exist_ok=True)
                lock_info = self.detect_profile_lock_markers(user_data_dir_obj)
                if lock_info.get("occupied"):
                    raise RuntimeError(
                        "检测到 user_data_dir 正在被占用（发现锁文件），请先关闭占用该 profile 的 Chrome/自动化：%s"
                        % ", ".join(lock_info.get("markers") or [])
                    )
                user_data_dir = str(user_data_dir_obj)

                chrome_options.add_argument(f'--user-data-dir={user_data_dir}')
                self.logger.info(f"使用用户数据目录保持登录状态: {user_data_dir}")
                self.logger.info("提示：如果Chrome浏览器正在运行，请先关闭后再启动系统")
            else:
                self.logger.warning("未配置用户数据目录，登录状态将不会保持")
                self.logger.warning("建议在config.yaml中配置 browser.user_data_dir")
            
            # 配置固定 chromedriver 后完全绕过 Selenium Manager。
            chromedriver_path = self._get_chromedriver_path()
            if chromedriver_path:
                self.logger.info("使用固定 chromedriver: %s", chromedriver_path)
                self.driver = webdriver.Chrome(
                    service=Service(executable_path=str(chromedriver_path)),
                    options=chrome_options,
                )
            else:
                self.logger.warning(
                    "未配置 browser.chromedriver_path，将使用 Selenium Manager，首次启动可能较慢"
                )
                self.driver = webdriver.Chrome(options=chrome_options)
            
            # 设置超时时间
            implicit_wait = self.browser_config.get('implicit_wait', 10)
            self.driver.implicitly_wait(implicit_wait)
            
            page_load_timeout = self.browser_config.get('page_load_timeout', 30)
            self.driver.set_page_load_timeout(page_load_timeout)

            # 降低 Selenium 自动化指纹（不破解验证码，仅减少 webdriver 暴露）
            self._apply_stealth_patches()
            
            # 打开一个空白页（避免显示data:,）
            self.driver.get("about:blank")
            
            self.logger.info("浏览器启动成功")
        
        except Exception as e:
            self.logger.error(f"浏览器启动失败: {e}")
            if "user data directory is already in use" in str(e).lower():
                self.logger.error("Chrome用户数据目录正在被使用，请关闭其他Chrome浏览器实例")
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
    
    def get_driver(self) -> webdriver.Chrome:
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

