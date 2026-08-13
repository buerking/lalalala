"""
重试机制工具模块
"""

import time
import functools
from typing import Callable, Type, Tuple, Any, Sequence, Optional
import logging

logger = logging.getLogger(__name__)

# 订单后台回调（checkCart / addedCart / addNo 等）网络类失败重试间隔：
# 第1次失败后立即再试 → 再失败等 1 分钟 → 再失败等 5 分钟（共 4 次尝试）
API_CALLBACK_RETRY_WAITS: Tuple[float, ...] = (0.0, 60.0, 300.0)


def is_transient_http_error(code: Optional[int] = None, error_message: str = "") -> bool:
    """判断是否为可重试的网络/瞬时失败（HTTP 0、5xx、超时等）。业务 Success=false 不在此列。"""
    if code is not None:
        try:
            c = int(code)
        except Exception:
            c = -1
        if c == 0 or c < 0 or c >= 500:
            return True
    msg = (error_message or "").lower()
    for kw in (
        "http 0",
        "超时",
        "timeout",
        "请求异常",
        "connection",
        "connect",
        "ssl",
        "curl",
        "reset",
        "refused",
        "unreachable",
        "temporarily",
        "timed out",
    ):
        if kw in msg:
            return True
    return False


def call_api_with_retries(
    label: str,
    attempt_fn: Callable[[int], Tuple[bool, bool, Any]],
    waits: Sequence[float] = API_CALLBACK_RETRY_WAITS,
    log: Callable[..., None] = print,
) -> Any:
    """
    对订单后台 API 做固定间隔重试。

    attempt_fn(attempt_index) -> (ok, retryable, result)
      - ok=True：成功，立即返回 result
      - ok=False 且 retryable=True：按 waits 等待后重试
      - ok=False 且 retryable=False：业务失败，立即返回 result（不空等）

    waits: 每次失败后、下一次尝试前的等待秒数（默认 0 / 60 / 300）。
    """
    wait_list = list(waits) if waits is not None else list(API_CALLBACK_RETRY_WAITS)
    total = 1 + len(wait_list)
    last_result: Any = None

    for i in range(total):
        attempt_no = i + 1
        if i > 0:
            delay = float(wait_list[i - 1])
            if delay > 0:
                log(
                    "[%s] 第%s次失败，%.0f 秒后重试（%s/%s）"
                    % (label, i, delay, attempt_no, total)
                )
                time.sleep(delay)
            else:
                log(
                    "[%s] 第%s次失败，立即重试（%s/%s）"
                    % (label, i, attempt_no, total)
                )
        try:
            ok, retryable, result = attempt_fn(attempt_no)
        except Exception as e:
            ok, retryable, result = False, True, e
            log("[%s] 第%s次尝试异常: %s" % (label, attempt_no, e))
        last_result = result
        if ok:
            if attempt_no > 1:
                log("[%s] 重试成功（第%s次尝试）" % (label, attempt_no))
            return result
        if not retryable:
            return result

    log("[%s] 已重试 %s 次仍失败" % (label, total))
    return last_result


def retry(
    max_attempts: int = 3,
    delay: float = 5.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Callable[[Exception, int], None] = None
):
    """
    重试装饰器
    
    Args:
        max_attempts: 最大尝试次数
        delay: 初始延迟时间（秒）
        backoff: 退避倍数（每次重试延迟时间 = delay * backoff^attempt）
        exceptions: 需要重试的异常类型
        on_retry: 重试时的回调函数，参数为(异常, 当前尝试次数)
    
    Example:
        @retry(max_attempts=3, delay=5.0)
        def my_function():
            # 可能失败的操作
            pass
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            current_delay = delay
            
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    
                    if attempt < max_attempts:
                        logger.warning(
                            f"函数 {func.__name__} 执行失败 (尝试 {attempt}/{max_attempts}): {e}"
                        )
                        
                        if on_retry:
                            try:
                                on_retry(e, attempt)
                            except Exception as callback_error:
                                logger.error(f"重试回调函数执行失败: {callback_error}")
                        
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(
                            f"函数 {func.__name__} 执行失败，已达到最大重试次数: {e}"
                        )
            
            # 所有重试都失败，抛出最后一个异常
            raise last_exception
        
        return wrapper
    return decorator


class RetryHandler:
    """重试处理器类"""
    
    def __init__(
        self,
        max_attempts: int = 3,
        delay: float = 5.0,
        backoff: float = 2.0,
        exceptions: Tuple[Type[Exception], ...] = (Exception,)
    ):
        self.max_attempts = max_attempts
        self.delay = delay
        self.backoff = backoff
        self.exceptions = exceptions
    
    def execute(self, func: Callable, *args, **kwargs) -> Any:
        """
        执行函数，带重试机制
        
        Args:
            func: 要执行的函数
            *args: 函数位置参数
            **kwargs: 函数关键字参数
            
        Returns:
            函数返回值
            
        Raises:
            最后一次尝试的异常
        """
        last_exception = None
        current_delay = self.delay
        
        for attempt in range(1, self.max_attempts + 1):
            try:
                return func(*args, **kwargs)
            except self.exceptions as e:
                last_exception = e
                
                if attempt < self.max_attempts:
                    logger.warning(
                        f"操作执行失败 (尝试 {attempt}/{self.max_attempts}): {e}"
                    )
                    time.sleep(current_delay)
                    current_delay *= self.backoff
                else:
                    logger.error(
                        f"操作执行失败，已达到最大重试次数: {e}"
                    )
        
        raise last_exception

