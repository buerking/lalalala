# -*- coding: utf-8 -*-
"""乐天市场：订单 Specification/SystemRemark 与购物车规格展示对照。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Sequence, Tuple

# 接口/页面常见的「轴名称」，不是规格取值，对照时丢掉
_LABEL_STOPWORDS = {
    "サイズ",
    "size",
    "sz",
    "カラー",
    "カラー",
    "color",
    "colour",
    "col",
    "色",
    "规格",
    "規格",
    "仕様",
    "タイプ",
    "type",
    "kind",
    "容量",
    "味",
    "香り",
    "柄",
    "素材",
    "モデル",
    "model",
    "variant",
    "sku",
    "option",
    "オプション",
    "参考",
    "其他参考",
    "備考",
    "备注",
}

_SPLIT_GROUPS = re.compile(r"[；;]+")
_SPLIT_ATTRS = re.compile(r"[，,、]+")
_SPLIT_KV = re.compile(r"[：:]")


def _norm(text: Any) -> str:
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _is_label_token(token: str, extra_labels: Iterable[str] = ()) -> bool:
    n = _norm(token)
    if not n:
        return True
    if n in {_norm(x) for x in _LABEL_STOPWORDS}:
        return True
    extra = {_norm(x) for x in extra_labels if str(x or "").strip()}
    if n in extra:
        return True
    # 纯中日文短标签（2～6 字）且出现在购物车左侧轴名里，上面 extra 已覆盖
    return False


def parse_spec_text(*parts: Any) -> Tuple[List[str], List[str]]:
    """
    拆 Specification / SystemRemark。
    返回 (values, labels)：values 是待核对的规格值，labels 是轴名（サイズ等）。
    """
    values: List[str] = []
    labels: List[str] = []
    seen_v = set()
    seen_l = set()

    def _add_value(raw: str) -> None:
        t = str(raw or "").strip()
        if not t:
            return
        n = _norm(t)
        if not n or n in seen_v:
            return
        seen_v.add(n)
        values.append(t)

    def _add_label(raw: str) -> None:
        t = str(raw or "").strip()
        if not t:
            return
        n = _norm(t)
        if not n or n in seen_l:
            return
        seen_l.add(n)
        labels.append(t)

    blobs: List[str] = []
    for p in parts:
        s = str(p or "").strip()
        if s:
            blobs.append(s)
    if not blobs:
        return [], []

    for blob in blobs:
        for group in _SPLIT_GROUPS.split(blob):
            group = group.strip()
            if not group:
                continue
            chunks = [c.strip() for c in _SPLIT_ATTRS.split(group) if c.strip()]
            if not chunks:
                continue
            for chunk in chunks:
                if _SPLIT_KV.search(chunk):
                    left, right = _SPLIT_KV.split(chunk, 1)
                    if left.strip():
                        _add_label(left)
                    if right.strip():
                        _add_value(right)
                else:
                    # 尚不知是轴名还是取值，先当值；对照时再按购物车轴名过滤
                    _add_value(chunk)
    return values, labels


def order_spec_raw(product: Dict[str, Any]) -> Tuple[str, str]:
    specs: List[str] = []
    remarks: List[str] = []

    def _take(d: Dict[str, Any]) -> None:
        s = str(d.get("specification") or "").strip()
        r = str(d.get("system_remark") or "").strip()
        if s and s not in specs:
            specs.append(s)
        if r and r not in remarks:
            remarks.append(r)

    if isinstance(product, dict):
        _take(product)
        for line in product.get("_source_lines") or []:
            if isinstance(line, dict):
                _take(line)
    return "；".join(specs), "；".join(remarks)


def expected_spec_values(product: Dict[str, Any], cart_labels: Sequence[str] = ()) -> List[str]:
    spec, remark = order_spec_raw(product)
    values, labels = parse_spec_text(spec, remark)
    extra_labels = list(labels) + list(cart_labels)
    out: List[str] = []
    seen = set()
    for v in values:
        if _is_label_token(v, extra_labels):
            continue
        n = _norm(v)
        if n in seen:
            continue
        seen.add(n)
        out.append(v)
    return out


def parse_cart_spec_texts(texts: Sequence[str]) -> Tuple[List[str], List[str]]:
    """购物车一行规格文案（サイズ：MEDIUM）→ (values, labels)。"""
    values: List[str] = []
    labels: List[str] = []
    for raw in texts or []:
        t = str(raw or "").replace("\n", "").strip()
        if not t:
            continue
        t = re.sub(r"\s+", "", t)
        vs, ls = parse_spec_text(t)
        for x in vs:
            if x not in values:
                values.append(x)
        for x in ls:
            if x not in labels:
                labels.append(x)
        if not vs and not ls and t:
            values.append(t)
    return values, labels


def spec_value_matches(expected: str, cart_values: Sequence[str]) -> bool:
    en = _norm(expected)
    if not en:
        return True
    cart_n = [_norm(x) for x in cart_values if str(x or "").strip()]
    if en in cart_n:
        return True
    for cn in cart_n:
        if not cn:
            continue
        # 购物车值更完整（MEDIUM / MEDIUM（M））或接口值带多余前缀
        if en in cn or cn in en:
            if min(len(en), len(cn)) >= 2:
                return True
    return False


def compare_order_and_cart_specs(
    product: Dict[str, Any],
    cart_texts: Sequence[str],
) -> Tuple[bool, str, List[str], List[str]]:
    """
    返回 (ok, reason, expected_values, cart_values)。
    ok=True 表示通过（含：订单规格为空、或过滤后无有效取值）。
    """
    cart_values, cart_labels = parse_cart_spec_texts(cart_texts)
    expected = expected_spec_values(product, cart_labels)
    if not expected:
        return True, "skip_empty", expected, cart_values
    missing = [v for v in expected if not spec_value_matches(v, cart_values)]
    if missing:
        return (
            False,
            "missing:%s" % ",".join(missing),
            expected,
            cart_values,
        )
    return True, "ok", expected, cart_values
