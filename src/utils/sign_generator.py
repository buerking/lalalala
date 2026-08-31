"""
签名生成器，与接口验签规则一致
"""

import hashlib
import json
from typing import Any, Dict


class SignGenerator:
    """签名生成器，与 JavaScript 版本行为一致"""

    def __init__(self, client_secret: str):
        self.client_secret = client_secret

    def sort_params(self, params: Dict[str, Any]) -> Dict[str, str]:
        """
        处理并排序参数：
        - 列表和字典转为紧凑 JSON 字符串（ensure_ascii=False 保持原始字符）
        - 布尔值转为 'true'/'false'
        - None 转为 'null'
        - 其他类型直接转为字符串
        最后按键名升序返回新字典
        """
        processed: Dict[str, str] = {}
        for key, value in params.items():
            if isinstance(value, (list, dict)):
                processed[key] = json.dumps(
                    value, separators=(',', ':'), ensure_ascii=False
                )
            elif isinstance(value, bool):
                processed[key] = "true" if value else "false"
            elif value is None:
                processed[key] = "null"
            else:
                processed[key] = str(value)

        sorted_keys = sorted(processed.keys())
        return {k: processed[k] for k in sorted_keys}

    def concatenate_params(self, sorted_params: Dict[str, str]) -> str:
        """拼接参数和密钥：key1=value1&key2=value2&...&key=client_secret"""
        parts = [f"{k}={v}" for k, v in sorted_params.items()]
        parts.append(f"key={self.client_secret}")
        return "&".join(parts)

    def debug_concat_string(
        self, params: Dict[str, Any], *, redact_secret: bool = True
    ) -> str:
        """与 generateSign 相同的拼接串；默认脱敏 key=，便于对照 JS concatenatedString。"""
        sorted_params = self.sort_params(params)
        parts = [f"{k}={v}" for k, v in sorted_params.items()]
        sec = self.client_secret or ""
        if redact_secret:
            parts.append("key=<redacted len=%s>" % len(sec))
        else:
            parts.append("key=%s" % sec)
        return "&".join(parts)

    def generate_sign(self, params: Dict[str, Any]) -> str:
        """
        生成签名步骤：
        1. 排序参数
        2. 拼接字符串
        3. 计算 MD5（UTF-8 编码）
        4. 转为大写
        """
        sorted_params = self.sort_params(params)
        concat_str = self.concatenate_params(sorted_params)
        md5 = hashlib.md5(concat_str.encode('utf-8')).hexdigest()
        return md5.upper()
