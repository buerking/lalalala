# -*- coding: utf-8 -*-
"""EDI 回调验签：密钥选择与失败判定（加购 / Mark 刷新等共用）。

拉单返回的 order.secret 常不可用；列表接口用的全局 secret 更稳。
与骏河屋约定一致：默认优先 global_secret。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

_LAST_GOOD_SECRET: str = ""
_LAST_GOOD_MODE: str = "omit_empty"


def is_sign_failure(message: str) -> bool:
    msg = (message or "").strip()
    return "验签" in msg or "sign" in msg.lower()


def remember_sign_strategy(
    order: Optional[Dict[str, Any]],
    *,
    secret: str,
    mode: Optional[str] = None,
) -> None:
    global _LAST_GOOD_SECRET, _LAST_GOOD_MODE
    sec = (secret or "").strip()
    if order is not None and sec:
        order["_sign_secret"] = sec
    if sec:
        _LAST_GOOD_SECRET = sec
    if mode in ("omit_empty", "full"):
        _LAST_GOOD_MODE = mode
        if order is not None:
            order["_sign_mode"] = mode


def iter_sign_modes(
    order: Optional[Dict[str, Any]] = None,
    api_config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    order = order or {}
    api_config = api_config or {}
    preferred = str(
        order.get("_sign_mode")
        or _LAST_GOOD_MODE
        or api_config.get("sign_mode_prefer")
        or "omit_empty"
    ).strip()
    if preferred not in ("omit_empty", "full"):
        preferred = "omit_empty"
    out: List[str] = []
    for m in (preferred, "omit_empty", "full"):
        if m not in out:
            out.append(m)
    return out


def iter_sign_secrets(
    order: Dict[str, Any], api_config: Dict[str, Any]
) -> List[Tuple[str, str]]:
    """
    返回 (label, secret)，优先最可能成功：
    verified → last_good → global（默认）→ order
    """
    global _LAST_GOOD_SECRET
    order = order or {}
    api_config = api_config or {}

    verified = str(order.get("_sign_secret") or "").strip()
    if verified:
        return [("verified_secret", verified)]

    prefer = str(api_config.get("sign_secret_prefer") or "global").strip().lower()
    order_sec = str(order.get("secret") or "").strip()
    global_sec = str(api_config.get("secret") or "").strip()
    last_sec = str(_LAST_GOOD_SECRET or "").strip()

    out: List[Tuple[str, str]] = []
    seen = set()

    def _add(label: str, sec: str) -> None:
        if not sec or sec in seen:
            return
        seen.add(sec)
        out.append((label, sec))

    if last_sec:
        if last_sec == global_sec:
            _add("global_secret", last_sec)
        elif last_sec == order_sec:
            _add("order_secret", last_sec)
        else:
            _add("last_good_secret", last_sec)

    if prefer in ("global", "config", "api"):
        _add("global_secret", global_sec)
        _add("order_secret", order_sec)
    else:
        _add("order_secret", order_sec)
        _add("global_secret", global_sec)

    return out


def pick_sign_secret(order: Dict[str, Any], api_config: Dict[str, Any]) -> str:
    """取当前最优先密钥（已验证 / 全局 / 订单）。"""
    secrets = iter_sign_secrets(order or {}, api_config or {})
    return secrets[0][1] if secrets else ""


def params_for_sign(params: Dict[str, str], mode: str = "omit_empty") -> Dict[str, str]:
    if mode == "full":
        return dict(params)
    return {k: v for k, v in params.items() if str(v) != ""}
