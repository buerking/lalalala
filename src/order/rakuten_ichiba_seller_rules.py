# -*- coding: utf-8 -*-
"""
乐天市场确认页卖家特殊要求：按商品 URL 决定改地址 / 改信用卡 / 全改。

规则文件（JSON，不写 YAML）：
  data/rakuten_ichiba_checkout_rules.json
模板：
  src/order/rakuten_ichiba_checkout_rules.example.json
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

ACTION_ADDRESS = "address"
ACTION_CARD = "card"
ACTION_BOTH = "both"

_ACTION_ALIASES = {
    "address": ACTION_ADDRESS,
    "addr": ACTION_ADDRESS,
    "改地址": ACTION_ADDRESS,
    "card": ACTION_CARD,
    "credit": ACTION_CARD,
    "creditcard": ACTION_CARD,
    "改信用卡": ACTION_CARD,
    "改卡": ACTION_CARD,
    "both": ACTION_BOTH,
    "all": ACTION_BOTH,
    "全改": ACTION_BOTH,
}

_PLACEHOLDER_URL_MARKERS = (
    "example_shop",
    "example_item",
    "把店铺",
    "把完整",
    "填这里",
    "example.com",
    "shop-id",
)

# SPA：Mastercard **** 8828；Bic 下拉：MASTER下4桁8828 / 下4桁 8828
_LAST4_RE = re.compile(r"(?:\*{2,}|下\s*4\s*桁|[xXｘＸ]{4}|末尾)\s*(\d{4})")

_RUNTIME_REL = Path("data") / "rakuten_ichiba_checkout_rules.json"
_EXAMPLE_NAME = "rakuten_ichiba_checkout_rules.example.json"

_cache: Dict[str, Any] = {"path": "", "mtime": None, "bundle": None}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def example_json_path() -> Path:
    return Path(__file__).resolve().parent / _EXAMPLE_NAME


def runtime_json_path() -> Path:
    return project_root() / _RUNTIME_REL


def ensure_runtime_json(logger=None) -> Path:
    """data/ 下没有规则文件时，从 example 拷一份，方便直接填 URL。"""
    dest = runtime_json_path()
    if dest.exists():
        return dest
    src = example_json_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        if logger:
            logger.info("乐天市场：已生成规则文件 %s（请把 _examples 复制进 rules 并填真实 URL）", dest)
    else:
        dest.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "restore_on_normal_order": True,
                    "defaults": {"address_id": "honda", "card_last4": "8828"},
                    "addresses": {},
                    "cards": {},
                    "rules": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if logger:
            logger.warning("乐天市场：未找到规则模板，已写入空规则文件 %s", dest)
    return dest


def _norm_last4(value: Any, default: str = "") -> str:
    s = re.sub(r"\D", "", str(value or "").strip())
    if len(s) >= 4:
        return s[-4:]
    return str(default or "").strip()[-4:] if default else ""


def _norm_action(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return ""
    return _ACTION_ALIASES.get(key) or _ACTION_ALIASES.get(str(value or "").strip()) or ""


_RESERVED_SHOP_SLUGS = {
    "www",
    "item",
    "books",
    "search",
    "cart",
    "checkout",
    "doc",
    "ichiba",
    "category",
    "event",
    "news",
    "help",
    "my",
    "order",
    "basket",
    "step",
}

_SHOP_PATH_RE = re.compile(r"rakuten\.co\.jp/+([^/?#]+)", re.I)
_BARE_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$", re.I)


def extract_ichiba_shop_ids(*texts: str) -> set:
    """
    从商品链接或规则针里取出店铺 ID。
    item.rakuten.co.jp/renet3/xxx 与 www.rakuten.co.jp/renet3/ 都是 renet3。
    """
    shops = set()
    for text in texts:
        raw = str(text or "").strip()
        if not raw:
            continue
        blobs = product_url_candidates(raw) or [raw.lower()]
        for blob in blobs:
            for m in _SHOP_PATH_RE.finditer(blob):
                slug = (m.group(1) or "").strip("/").lower()
                if slug and slug not in _RESERVED_SHOP_SLUGS:
                    shops.add(slug)
        low = raw.lower().strip().strip("/")
        if _BARE_SHOP_RE.fullmatch(low) and low not in _RESERVED_SHOP_SLUGS:
            shops.add(low)
        elif _SHOP_PATH_RE.search(low) is None and "/" in low:
            first = low.split("/")[0]
            if _BARE_SHOP_RE.fullmatch(first) and first not in _RESERVED_SHOP_SLUGS:
                shops.add(first)
    return shops


def product_url_candidates(url: str) -> List[str]:
    """原始链接、解码、联盟 pc/m、去 query，用于 url_contains 匹配。"""
    raw = str(url or "").strip()
    if not raw:
        return []
    out: List[str] = [raw]
    try:
        out.append(unquote(raw))
    except Exception:
        pass
    try:
        parsed = urlparse(raw)
        host = (parsed.netloc or "").lower()
        if "afl.rakuten.co.jp" in host:
            qs = parse_qs(parsed.query)
            for key in ("pc", "m"):
                val = (qs.get(key) or [""])[0].strip()
                if not val:
                    continue
                out.append(val)
                try:
                    out.append(unquote(val))
                except Exception:
                    pass
    except Exception:
        pass
    expanded: List[str] = []
    for u in out:
        u = (u or "").strip()
        if not u:
            continue
        expanded.append(u)
        try:
            p = urlparse(u)
            host_path = ("%s%s" % (p.netloc or "", p.path or "")).rstrip("/")
            if host_path:
                expanded.append(host_path)
            no_q = urlparse(u)._replace(query="", fragment="").geturl().rstrip("/")
            if no_q:
                expanded.append(no_q)
        except Exception:
            pass
    seen = set()
    uniq: List[str] = []
    for u in expanded:
        key = u.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    return uniq


def _is_placeholder_pattern(text: str) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in _PLACEHOLDER_URL_MARKERS)


@dataclass
class AddressSpec:
    address_id: str
    label: str
    postal_code: str
    match_texts: List[str]


@dataclass
class CheckoutIntent:
    """确认页最终要落到的地址/卡。无规则命中时即为默认本田 + 8828。"""

    change_address: bool
    change_card: bool
    address: Optional[AddressSpec]
    card_last4: str
    matched_rule_ids: List[str] = field(default_factory=list)
    matched_urls: List[str] = field(default_factory=list)
    restore: bool = False
    skipped: bool = False
    skip_reason: str = ""
    warnings: List[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.skipped:
            return "跳过（%s）" % (self.skip_reason or "disabled")
        addr = (self.address.address_id if self.address else "-")
        kinds = []
        if self.change_address:
            kinds.append("地址=%s" % addr)
        if self.change_card:
            kinds.append("卡=%s" % self.card_last4)
        prefix = "恢复默认" if self.restore else "特殊要求"
        rules = ",".join(self.matched_rule_ids) or "-"
        return "%s %s rules=%s" % (prefix, "+".join(kinds) or "无改动", rules)


class SellerRulesBundle:
    def __init__(self, raw: Dict[str, Any], path: str = ""):
        self.raw = raw or {}
        self.path = path
        self.enabled = bool(self.raw.get("enabled", True))
        self.restore_on_normal_order = bool(self.raw.get("restore_on_normal_order", True))
        defaults = self.raw.get("defaults") or {}
        self.default_address_id = str(defaults.get("address_id") or "honda").strip() or "honda"
        self.default_card_last4 = _norm_last4(defaults.get("card_last4"), "8828") or "8828"
        self.addresses: Dict[str, AddressSpec] = {}
        for key, spec in (self.raw.get("addresses") or {}).items():
            if not isinstance(spec, dict):
                continue
            aid = str(spec.get("id") or key).strip()
            texts = [
                str(t).strip()
                for t in (spec.get("match_texts") or [])
                if str(t).strip()
            ]
            postal = str(spec.get("postal_code") or "").strip()
            if postal and postal not in texts:
                texts.append(postal)
            self.addresses[aid] = AddressSpec(
                address_id=aid,
                label=str(spec.get("label") or aid).strip(),
                postal_code=postal,
                match_texts=texts,
            )
        self.allowed_special_cards = set()
        for key, spec in (self.raw.get("cards") or {}).items():
            last4 = _norm_last4((spec or {}).get("last4") if isinstance(spec, dict) else spec, key)
            if last4 and last4 != self.default_card_last4:
                self.allowed_special_cards.add(last4)
        if not self.allowed_special_cards:
            self.allowed_special_cards = {"6667", "3636", "3646"}
        self.rules: List[Dict[str, Any]] = [
            r for r in (self.raw.get("rules") or []) if isinstance(r, dict)
        ]

    def address_spec(self, address_id: str) -> Optional[AddressSpec]:
        return self.addresses.get(str(address_id or "").strip())

    def resolve(self, product_urls: Iterable[str]) -> CheckoutIntent:
        if not self.enabled:
            return CheckoutIntent(
                change_address=False,
                change_card=False,
                address=self.address_spec(self.default_address_id),
                card_last4=self.default_card_last4,
                skipped=True,
                skip_reason="rules.enabled=false",
            )

        url_list = [str(u).strip() for u in (product_urls or []) if str(u).strip()]
        haystacks: List[Tuple[str, str]] = []
        for url in url_list:
            blob = " ".join(product_url_candidates(url))
            haystacks.append((url, blob))

        matched: List[Dict[str, Any]] = []
        matched_urls: List[str] = []
        for rule in self.rules:
            if rule.get("enabled") is False:
                continue
            action = _norm_action(rule.get("action"))
            if action not in (ACTION_ADDRESS, ACTION_CARD, ACTION_BOTH):
                continue
            hit_url = self._rule_hits(rule, haystacks)
            if not hit_url:
                continue
            matched.append(rule)
            if hit_url not in matched_urls:
                matched_urls.append(hit_url)

        default_addr = self.address_spec(self.default_address_id)
        if not matched:
            if not self.restore_on_normal_order:
                return CheckoutIntent(
                    change_address=False,
                    change_card=False,
                    address=default_addr,
                    card_last4=self.default_card_last4,
                    skipped=True,
                    skip_reason="无规则命中且 restore_on_normal_order=false",
                    restore=True,
                )
            return CheckoutIntent(
                change_address=True,
                change_card=True,
                address=default_addr,
                card_last4=self.default_card_last4,
                restore=True,
            )

        want_address = False
        want_card = False
        address_id = self.default_address_id
        card_last4 = self.default_card_last4
        rule_ids: List[str] = []
        card_from: List[str] = []
        warnings: List[str] = []
        for rule in matched:
            rid = str(rule.get("id") or "").strip() or "(no-id)"
            rule_ids.append(rid)
            action = _norm_action(rule.get("action"))
            if action in (ACTION_ADDRESS, ACTION_BOTH):
                want_address = True
                aid = str(rule.get("address_id") or "nihonbashi").strip() or "nihonbashi"
                address_id = aid
            if action in (ACTION_CARD, ACTION_BOTH):
                want_card = True
                last4 = _norm_last4(rule.get("card_last4"))
                if last4:
                    if card_from and last4 not in card_from:
                        warnings.append(
                            "多条规则指定了不同信用卡尾号 %s，采用 %s（规则 %s）"
                            % ("/".join(card_from + [last4]), last4, rid)
                        )
                    card_last4 = last4
                    if last4 not in card_from:
                        card_from.append(last4)

        # 特殊单只改地址时，卡仍应回到默认 8828（避免上一单特殊卡残留）
        # 特殊单只改卡时，地址仍应回到本田
        if self.restore_on_normal_order:
            if not want_address:
                want_address = True
                address_id = self.default_address_id
            if not want_card:
                want_card = True
                card_last4 = self.default_card_last4

        addr = self.address_spec(address_id) or default_addr
        return CheckoutIntent(
            change_address=want_address,
            change_card=want_card,
            address=addr,
            card_last4=card_last4 or self.default_card_last4,
            matched_rule_ids=rule_ids,
            matched_urls=matched_urls,
            restore=False,
            warnings=warnings,
        )

    def _rule_hits(
        self, rule: Dict[str, Any], haystacks: List[Tuple[str, str]]
    ) -> str:
        contains = [
            str(x).strip()
            for x in (rule.get("url_contains") or [])
            if str(x).strip() and not _is_placeholder_pattern(str(x))
        ]
        regexes = [
            str(x).strip()
            for x in (rule.get("url_regex") or [])
            if str(x).strip() and not _is_placeholder_pattern(str(x))
        ]
        if not contains and not regexes:
            return ""
        compiled: List[re.Pattern] = []
        for pat in regexes:
            try:
                compiled.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                continue
        for url, blob in haystacks:
            product_shops = extract_ichiba_shop_ids(url, blob)
            for needle in contains:
                needle_shops = extract_ichiba_shop_ids(needle)
                if needle_shops and (product_shops & needle_shops):
                    return url
                if not needle_shops and needle.lower() in blob:
                    return url
            for cre in compiled:
                if cre.search(blob) or cre.search(url):
                    return url
        return ""


def load_seller_rules(logger=None) -> SellerRulesBundle:
    path = ensure_runtime_json(logger=logger)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    key = str(path)
    if (
        _cache.get("bundle") is not None
        and _cache.get("path") == key
        and _cache.get("mtime") == mtime
    ):
        return _cache["bundle"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("规则文件根节点必须是 JSON 对象")
    except Exception as e:
        if logger:
            logger.error("乐天市场：读取卖家规则失败 %s: %s", path, e)
        raw = {
            "enabled": False,
            "restore_on_normal_order": True,
            "defaults": {"address_id": "honda", "card_last4": "8828"},
            "addresses": {},
            "cards": {},
            "rules": [],
        }
    bundle = SellerRulesBundle(raw, path=str(path))
    _cache["path"] = key
    _cache["mtime"] = mtime
    _cache["bundle"] = bundle
    return bundle


def extract_card_last4_from_text(text: str) -> str:
    m = _LAST4_RE.search(text or "")
    return m.group(1) if m else ""
