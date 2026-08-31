# -*- coding: utf-8 -*-
"""EDI 回调验签：密钥选择与失败判定（加购 / Mark 刷新等共用）。

单订单回调优先使用 getOrderListSimple 返回的 order.secret（见 docs/run_flow_detailed.md）；
仅当订单未带 Secret 时回退 order_api.secret。
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
    返回 (label, secret)。

    与 run_flow_detailed 约定一致：单订单回调优先 order.secret，
    仅当订单未带 Secret 时才用配置 order_api.secret。
    可用 order_api.sign_secret_prefer=global 覆盖（少数站点）。
    """
    global _LAST_GOOD_SECRET
    order = order or {}
    api_config = api_config or {}

    verified = str(order.get("_sign_secret") or "").strip()
    if verified:
        return [("verified_secret", verified)]

    # 文档约定默认 order；旧逻辑误改为 global 会导致与拉单 Secret 不一致
    prefer = str(api_config.get("sign_secret_prefer") or "order").strip().lower()
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

    # last_good 仅在与 prefer 同侧时提前，避免跨单/跨站把错误密钥顶到最前
    if last_sec:
        if prefer in ("global", "config", "api") and last_sec == global_sec:
            _add("global_secret", last_sec)
        elif prefer not in ("global", "config", "api") and last_sec == order_sec:
            _add("order_secret", last_sec)

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
