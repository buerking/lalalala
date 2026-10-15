# -*- coding: utf-8 -*-
"""
拉单后的禁售商品中间件：用标题 / URL / 货号 / 规格 / 备注做关键词匹配。

乐天 getOrderListSimple 的 name 经常是 GoodsNo，没有日文标题，
所以必须同时扫商品 URL 和店铺 slug（例如 item.rakuten.co.jp/iqos/7904）。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

# 先从文本里挖掉这些，再匹配，减少「醤油」「ハイライター」误伤。
_DEFAULT_EXCLUDES = (
    "醤油",
    "醬油",
    "酒粕",
    "酒盗",
    "ハイライター",
    "蛍光ペン",
    "コピーライター",
    "ゴーストライター",
    "スクリーンライター",
    "消毒用アルコール",
    "アルコール消毒液",
    "除菌アルコール",
    "アルコールランプ",
    "イソプロピル",
    "エタノール",
)

# 分类 -> 关键词。大小写不敏感；日文按原文子串匹配。
_DEFAULT_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "电子烟/烟草": (
        "iqos",
        "アイコス",
        "iluma",
        "イルマ",
        "glo",
        "グロー",
        "ploom",
        "プルーム",
        "プルームテック",
        "ploomtech",
        "加熱式タバコ",
        "加熱式たばこ",
        "加熱式煙草",
        "電子タバコ",
        "電子たばこ",
        "電子煙草",
        "電タバ",
        "vape",
        "ベイプ",
        "ニコチン",
        "nicotine",
        "terea",
        "テリア",
        "sentia",
        "センティア",
        "heets",
        "ヒーツ",
        "たばこスティック",
        "タバコスティック",
        "紙巻きたばこ",
        "紙巻タバコ",
        "葉巻",
        "シガー",
        "cigar",
        "cigarette",
        "煙草",
        "タバコ",
        "たばこ",
        "喫煙具",
        "ニコチンリキッド",
        "ベイプリキッド",
        "シーシャ",
        "水タバコ",
        "水たばこ",
        "フレーバーポッド",
        "philipmorris",
        "フィリップモリス",
    ),
    "酒类": (
        "日本酒",
        "焼酎",
        "泡盛",
        "ウイスキー",
        "ウィスキー",
        "whisky",
        "whiskey",
        "ブランデー",
        "brandy",
        "ウォッカ",
        "vodka",
        "ワイン",
        "wine",
        "シャンパン",
        "champagne",
        "ビール",
        "beer",
        "梅酒",
        "果実酒",
        "リキュール",
        "liqueur",
        "清酒",
        "濁酒",
        "どぶろく",
        "紹興酒",
        "黄酒",
        "酒類",
        "お酒",
        "料理酒",
        "アルコール度数",
        "チューハイ",
        "ハイボール",
        "サワー缶",
        "alc.",
        "アルコール",
        "日本酒",
        "酒",
    ),
    "打火机": (
        "zippo",
        "ジッポー",
        "ジッポ",
        "オイルライター",
        "ガスライター",
        "ターボライター",
        "バーナーライター",
        "ライターオイル",
        "着火石",
        "フリント",
        "ライター",
    ),
    "刀具": (
        "サバイバルナイフ",
        "フォールディングナイフ",
        "折りたたみナイフ",
        "シースナイフ",
        "カッターナイフ",
        "三徳包丁",
        "刺身包丁",
        "出刃包丁",
        "菜切包丁",
        "包丁",
        "日本刀",
        "模造刀",
        "居合刀",
        "脇差",
        "短刀",
        "刃物",
        "刃渡り",
        "dagger",
        "ナイフ",
        "出刃",
        "鉈",
        "剣",
    ),
}

_lock = threading.Lock()


@dataclass(frozen=True)
class ProhibitedHit:
    category: str
    keyword: str
    field: str
    snippet: str


def _section(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    sec = config.get("prohibited_items")
    return sec if isinstance(sec, dict) else {}


def is_enabled(config: Optional[Dict[str, Any]]) -> bool:
    sec = _section(config)
    if "enabled" not in sec:
        return True
    return bool(sec.get("enabled"))


def _categories(config: Optional[Dict[str, Any]]) -> Dict[str, Tuple[str, ...]]:
    sec = _section(config)
    extra = sec.get("extra_keywords") if isinstance(sec.get("extra_keywords"), dict) else {}
    out: Dict[str, Tuple[str, ...]] = {}
    names = list(_DEFAULT_CATEGORIES.keys())
    for k in extra:
        if k not in names:
            names.append(str(k))
    for name in names:
        base = list(_DEFAULT_CATEGORIES.get(name) or ())
        more = extra.get(name) if isinstance(extra.get(name), list) else []
        merged: List[str] = []
        seen = set()
        for kw in list(base) + [str(x) for x in more]:
            s = str(kw or "").strip()
            if not s:
                continue
            key = s.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(s)
        if merged:
            out[name] = tuple(merged)
    return out


def _excludes(config: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    sec = _section(config)
    extra = sec.get("extra_excludes") if isinstance(sec.get("extra_excludes"), list) else []
    merged: List[str] = []
    seen = set()
    for x in list(_DEFAULT_EXCLUDES) + [str(i) for i in extra]:
        s = str(x or "").strip()
        if not s:
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        merged.append(s)
    return tuple(merged)


def _rakuten_shop_slug(url: str) -> str:
    try:
        path = unquote(urlparse(url).path or "")
    except Exception:
        return ""
    parts = [p for p in path.split("/") if p]
    # /iqos/7904/ 或 /iqos/7904.html
    if len(parts) >= 1 and "rakuten.co.jp" in (urlparse(url).netloc or "").lower():
        return parts[0]
    return ""


def product_haystack(product: Dict[str, Any]) -> List[Tuple[str, str]]:
    """返回 (字段名, 原文) 供匹配与飞书展示。"""
    url = str(product.get("url") or "").strip()
    fields = [
        ("name", str(product.get("name") or "")),
        ("url", url),
        ("goods_no", str(product.get("goods_no") or "")),
        ("specification", str(product.get("specification") or "")),
        ("system_remark", str(product.get("system_remark") or "")),
    ]
    shop = _rakuten_shop_slug(url)
    if shop:
        fields.append(("shop", shop))
    return [(k, v) for k, v in fields if str(v).strip()]


def _scrub(text: str, excludes: Tuple[str, ...]) -> str:
    out = str(text or "")
    for ex in excludes:
        if not ex:
            continue
        out = re.sub(re.escape(ex), " ", out, flags=re.IGNORECASE)
    return out


def match_text(
    text: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    field: str = "text",
) -> Optional[ProhibitedHit]:
    raw = str(text or "")
    if not raw.strip():
        return None
    excludes = _excludes(config)
    hay = _scrub(raw, excludes)
    hay_cf = hay.casefold()
    for category, keywords in _categories(config).items():
        for kw in keywords:
            needle = kw.casefold()
            if needle and needle in hay_cf:
                snippet = raw.replace("\n", " ").strip()
                if len(snippet) > 180:
                    snippet = snippet[:180] + "…"
                return ProhibitedHit(category, kw, field, snippet)
    return None


def inspect_order(
    order: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Optional[ProhibitedHit]:
    if not is_enabled(config):
        return None
    for product in (order or {}).get("products") or []:
        if not isinstance(product, dict):
            continue
        for field, text in product_haystack(product):
            hit = match_text(text, config, field=field)
            if hit:
                return hit
    return None


def split_orders(
    orders: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], ProhibitedHit]]]:
    kept: List[Dict[str, Any]] = []
    blocked: List[Tuple[Dict[str, Any], ProhibitedHit]] = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        hit = inspect_order(order, config)
        if hit:
            blocked.append((order, hit))
        else:
            kept.append(order)
    return kept, blocked


def _memory_path(config: Optional[Dict[str, Any]]) -> str:
    sec = _section(config)
    path = str(sec.get("notified_file") or "").strip() or "data/prohibited_notified.json"
    root = Path(__file__).resolve().parent.parent.parent
    if not os.path.isabs(path):
        path = str((root / path).resolve())
    return path


def was_notified(config: Optional[Dict[str, Any]], order_id: str) -> bool:
    oid = str(order_id or "").strip()
    if not oid:
        return False
    path = _memory_path(config)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return False
    ids = data.get("order_ids") or []
    return oid in {str(x) for x in ids}


def mark_notified(config: Optional[Dict[str, Any]], order_id: str) -> None:
    oid = str(order_id or "").strip()
    if not oid:
        return
    path = _memory_path(config)
    with _lock:
        data: Dict[str, Any] = {"order_ids": []}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {"order_ids": []}
            except Exception:
                data = {"order_ids": []}
        ids = [str(x) for x in (data.get("order_ids") or []) if str(x).strip()]
        if oid not in ids:
            ids.append(oid)
        data["order_ids"] = ids[-500:]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
