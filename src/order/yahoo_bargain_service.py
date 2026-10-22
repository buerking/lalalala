# -*- coding: utf-8 -*-
"""
雅虎闲置议价：
- 每轮定时任务开头盯本地队列（默认 24h 开商品页，之后改商品 API，满 72h 超时并删单退款）
- 普通拉单结束后拉 getBargainOrderListSimple，提交「価格の相談」
- 提交时找不到议价窗口：飞书提醒后改为 API 盯价，不再反复打开商品页
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.notification.feishu_notifier import FeishuNotifier
from src.order.cancel_order import (
    BARGAIN_TIMEOUT_CANCEL_REASON,
    auto_cancel_and_notify,
)
from src.order.order_fetcher import OrderFetcher
from src.order.yahoo_bargain_store import YahooBargainStore, _now_iso
from src.order.yahoo_fleamarket_processor import (
    YahooFleaMarketOrderProcessor,
    extract_yahoo_item_id,
)
from src.utils.logger import LoggerMixin


def _jsonable(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return None


_UNSOLD = {
    "SOLD",
    "CLOSE",
    "CLOSED",
    "CANCELLED",
    "CANCELED",
    "DELETED",
    "STOP",
    "STOPPED",
}


def _dig(data: Any, key: str) -> Any:
    if not isinstance(data, dict):
        return None
    if key in data and data.get(key) is not None:
        return data.get(key)
    item = data.get("item")
    if isinstance(item, dict) and item.get(key) is not None:
        return item.get(key)
    return None


def _yen_int(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        v = int(round(float(str(raw).replace(",", "").strip())))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def item_listed_price(data: Optional[dict]) -> Optional[int]:
    if not data:
        return None
    for key in ("price", "sellPrice", "currentPrice"):
        n = _yen_int(_dig(data, key))
        if n:
            return n
    return None


def item_is_on_sale(data: Optional[dict]) -> bool:
    if not data:
        return False
    st = str(_dig(data, "status") or "").strip().upper()
    if st in _UNSOLD:
        return False
    return True


def _image_urls(data: Optional[dict]) -> List[str]:
    raw = _dig(data, "images") or []
    urls: List[str] = []
    if isinstance(raw, list):
        for x in raw:
            u = ""
            if isinstance(x, str):
                u = x
            elif isinstance(x, dict):
                u = str(x.get("url") or x.get("src") or "")
            u = u.split("?")[0].strip()
            if u:
                urls.append(u)
    return urls


def _norm_text(v: Any) -> str:
    return " ".join(str(v or "").split())


_FW_DIGITS = "０１２３４５６７８９"
_HW_DIGITS = "0123456789"
_TITLE_PUNCT_RE = re.compile(r"[\s　\-_/・.,，。:：;；【】\[\]()（）]")


def _compact_title(v: Any) -> str:
    t = _norm_text(v).casefold()
    t = t.translate(str.maketrans(_FW_DIGITS, _HW_DIGITS))
    return _TITLE_PUNCT_RE.sub("", t)


def titles_equivalent(a: Any, b: Any) -> bool:
    """拍品页 h1 与 API 标题可能差空格/全角；不是换商品。"""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ca, cb = _compact_title(na), _compact_title(nb)
    if not ca or not cb:
        return False
    return ca == cb or ca in cb or cb in ca


def build_item_snapshot(data: Optional[dict], item_id: str) -> Dict[str, Any]:
    seller = _dig(data, "seller")
    seller_id = ""
    if isinstance(seller, dict):
        seller_id = str(seller.get("id") or "")
    elif seller is not None:
        seller_id = str(seller)
    cond = _dig(data, "condition")
    cond_key = ""
    if isinstance(cond, dict):
        cond_key = str(cond.get("key") or cond.get("text") or "")
    elif cond is not None:
        cond_key = str(cond)
    return {
        "item_id": item_id,
        "status": str(_dig(data, "status") or ""),
        "price": item_listed_price(data),
        "title": _norm_text(_dig(data, "title")),
        "description": _norm_text(_dig(data, "description")),
        "images": _image_urls(data),
        "condition": cond_key,
        "seller_id": seller_id,
        "jan": str(_dig(data, "jan") or ""),
    }


def snapshots_match(saved: Optional[dict], current: Optional[dict]) -> Tuple[bool, str]:
    if not saved or not current:
        return False, "缺少商品快照"
    checks = (
        ("title", "标题"),
        ("description", "描述"),
        ("condition", "成色"),
        ("seller_id", "卖家"),
        ("jan", "JAN"),
    )
    for key, label in checks:
        a = _norm_text(saved.get(key))
        b = _norm_text(current.get(key))
        if a and b and a != b:
            return False, "%s不一致" % label
        if a and not b:
            return False, "%s缺失" % label
    sa = set(saved.get("images") or [])
    sb = set(current.get("images") or [])
    if sa and sb and sa != sb:
        return False, "商品图片不一致"
    return True, ""


def _parse_iso(s: Any) -> Optional[datetime]:
    text = str(s or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def record_to_order(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "order_id": rec.get("order_id"),
        "order_no": rec.get("order_no") or rec.get("order_id"),
        "user_id": rec.get("user_id"),
        "mark": rec.get("mark"),
        "secret": rec.get("secret"),
        "products": rec.get("products") or [],
        "service_type_ids": rec.get("service_type_ids") or [],
        "from_bargain": True,
    }


class YahooBargainService(LoggerMixin):
    def __init__(
        self,
        config: Dict[str, Any],
        yahoo_processor: YahooFleaMarketOrderProcessor,
        process_order: Callable[[Dict[str, Any]], Tuple[bool, Dict[str, Any]]],
        cooldown: Optional[Callable[[], None]] = None,
    ):
        self.config = config
        self.y_cfg = config.get("yahoo_fleamarket") or {}
        self.yp = yahoo_processor
        self._process_order = process_order
        self._cooldown = cooldown
        self.store = YahooBargainStore(config)
        self.fetcher = OrderFetcher(config)
        self.feishu = FeishuNotifier(config)

    def enabled_submit(self) -> bool:
        return bool(self.y_cfg.get("bargain_submit_enabled", True))

    def enabled_monitor(self) -> bool:
        return bool(self.y_cfg.get("bargain_monitor_enabled", True))

    def _timeout_hours(self) -> float:
        """盯价上限：默认 72 小时。不以 bargain_max_age_days×24 为准（7 天会变成 168）。"""
        hours = self.y_cfg.get("bargain_timeout_hours")
        if hours is not None and str(hours).strip() != "":
            try:
                return max(1.0, float(hours))
            except (TypeError, ValueError):
                pass
        return 72.0

    def _page_watch_hours(self) -> float:
        """开商品页盯价时长，默认 24 小时；超时后改走商品 API。"""
        hours = self.y_cfg.get("bargain_page_watch_hours", 24)
        try:
            return max(0.0, float(hours))
        except (TypeError, ValueError):
            return 24.0

    def _submit_max_attempts(self) -> int:
        """协商页提交失败最多重试几次（含首次）。默认 3，避免每 10 秒无限重试刷飞书。"""
        raw = self.y_cfg.get("bargain_submit_max_attempts", 3)
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            return 3

    @staticmethod
    def _record_item_id(rec: Dict[str, Any]) -> str:
        products = rec.get("products") or []
        if not products:
            return ""
        return (extract_yahoo_item_id(str(products[0].get("url") or "")) or "").strip()

    def _active_other_for_item(
        self, item_id: str, exclude_oid: str
    ) -> Optional[Dict[str, Any]]:
        iid = (item_id or "").strip().lower()
        if not iid:
            return None
        ex = str(exclude_oid or "").strip()
        for rec in self.store.list_active():
            oid = str(rec.get("order_id") or "").strip()
            if not oid or oid == ex:
                continue
            other = self._record_item_id(rec).lower()
            if other and other == iid:
                return rec
        return None

    def _watch_start(self, rec: Dict[str, Any]) -> Optional[datetime]:
        start = _parse_iso(rec.get("bargain_submitted_at")) or _parse_iso(
            rec.get("fetched_at")
        )
        if start is None:
            return None
        now = datetime.now().astimezone()
        if start.tzinfo is None:
            start = start.replace(tzinfo=now.tzinfo)
        return start

    def _is_timeout(self, rec: Dict[str, Any]) -> bool:
        hours = self._timeout_hours()
        start = self._watch_start(rec)
        if start is None:
            return False
        now = datetime.now().astimezone()
        return now - start >= timedelta(hours=hours)

    def _use_page_watch(self, rec: Dict[str, Any]) -> bool:
        mode = str(rec.get("watch_mode") or "").strip().lower()
        if mode == "api":
            return False
        start = self._watch_start(rec)
        if start is None:
            return True
        now = datetime.now().astimezone()
        return (now - start) < timedelta(hours=self._page_watch_hours())

    def _switch_to_api_watch(
        self, rec: Dict[str, Any], note: str, messages: List[str]
    ) -> None:
        """找不到议价窗口：飞书一次，转入 API 盯价，不再打开商品页提交。"""
        oid = str(rec.get("order_id") or "")
        rec["status"] = "watching"
        rec["watch_mode"] = "api"
        rec["bargain_submitted_at"] = rec.get("bargain_submitted_at") or _now_iso()
        rec["note"] = note
        already = bool(rec.get("api_watch_notified"))
        rec["api_watch_notified"] = True
        self.store.upsert(rec)
        self.logger.warning(
            "雅虎闲置议价：未找到议价窗口，改为 API 盯价 order=%s %s", oid, note
        )
        if already:
            return
        extra = (
            "未找到议价操作窗口，已改为商品 API 盯价（不再打开商品页提交）；"
            "标价降到议价金额及以下时再自动购买"
        )
        self._notify(rec, messages, extra)

    def _notify(self, rec: Dict[str, Any], messages: List[str], reason: str) -> None:
        order = record_to_order(rec)
        try:
            self.feishu.notify_order_issue(
                str(order.get("order_id") or ""),
                messages,
                user_id=order.get("user_id"),
                extra=reason,
            )
        except Exception as e:
            self.logger.warning("雅虎闲置议价：飞书失败: %s", e)

    def _consume(self, rec: Dict[str, Any], status: str, note: str, messages: List[str]) -> None:
        oid = str(rec.get("order_id") or "")
        self.store.consume(oid, status, note=note)
        self.logger.info("雅虎闲置议价：已消费 order=%s status=%s %s", oid, status, note)
        if status in ("time_out", "lost") and messages:
            extra = "雅虎闲置议价 %s" % status
            if status == "lost" and any("不支持议价" in str(m) for m in messages):
                extra = "商品不支持议价，已停止，不再重试"
            self._notify(rec, messages, extra)

    def _consume_timeout(self, rec: Dict[str, Any], hours: float) -> None:
        """未买成且超过盯价时限：本地标 time_out，并调用删单退款。"""
        oid = str(rec.get("order_id") or "")
        note = "超过 %s 小时未达成议价，已请求删单退款" % hours
        msgs = [
            "议价超时（%s 小时）未成交，自动删单退款" % hours,
            "订单号 %s 议价金额 %s" % (rec.get("order_no") or oid, rec.get("bargain_price")),
        ]
        self.store.consume(oid, "time_out", note=note)
        self.logger.info("雅虎闲置议价：已消费 order=%s status=time_out %s", oid, note)
        auto_cancel_and_notify(
            record_to_order(rec),
            self.config,
            msgs,
            reason=BARGAIN_TIMEOUT_CANCEL_REASON,
            feishu_notifier=self.feishu,
            logger=self.logger,
            human_reason="议价超时自动删单失败，请人工删单退款。",
        )

    @staticmethod
    def _should_drop_unsupported_bargain(msg: str) -> bool:
        text = str(msg or "")
        hints = (
            "不支持议价",
            "未找到议价窗口",
            "未找到价格相談",
            "无価格の相談",
            "议价窗口内未找到金额输入框",
            "议价窗口内未找到提交按钮",
        )
        return any(h in text for h in hints)

    def _dev_accept_unverified_submit(self, rec: Dict[str, Any]) -> bool:
        """本地测试：未登录也可把 pending_submit 当成已提交，进入盯价。"""
        from src.utils.dev_test import skip_bargain_submit_verify

        if not skip_bargain_submit_verify(self.config):
            return False
        if str(rec.get("status") or "") != "pending_submit":
            return False
        rec["status"] = "watching"
        rec["watch_mode"] = rec.get("watch_mode") or "page"
        rec["bargain_submitted_at"] = rec.get("bargain_submitted_at") or _now_iso()
        rec["note"] = "本地测试：不验证议价提交成功，直接进入盯价"
        self.store.upsert(rec)
        self.logger.warning(
            "本地测试：order=%s 跳过登录/协商页校验，进入盯价队列",
            rec.get("order_id"),
        )
        return True

    def monitor_and_purchase(self) -> None:
        """每轮定时任务最前面：处理本地已提交的议价单。"""
        if not self.enabled_monitor():
            return
        for rec in list(self.store.list_active()):
            self._dev_accept_unverified_submit(rec)
        actives = self.store.list_active()
        if not actives:
            self.logger.info("雅虎闲置议价：本地队列无待处理记录")
            return
        hours = self._timeout_hours()
        watching = []
        pending = []
        for rec in actives:
            oid = str(rec.get("order_id") or "")
            st = str(rec.get("status") or "")
            try:
                if self._is_timeout(rec):
                    self._consume_timeout(rec, hours)
                    continue
            except Exception as e:
                self.logger.error(
                    "雅虎闲置议价：超时检查异常 order=%s: %s", oid, e, exc_info=True
                )
            if st == "watching":
                watching.append(rec)
            elif st == "pending_submit":
                pending.append(rec)
        if pending:
            self.logger.info(
                "雅虎闲置议价：本地 pending_submit %s 条（提交尚未确认成功，"
                "本轮不盯价、不比对现价，稍后会重新打开议价页尝试提交）",
                len(pending),
            )
            for rec in pending:
                self.logger.info(
                    "雅虎闲置议价：pending order=%s note=%s",
                    rec.get("order_no") or rec.get("order_id"),
                    rec.get("note") or "",
                )
        if not watching:
            if pending:
                self.logger.info("雅虎闲置议价：无 watching 记录，跳过盯价")
            else:
                self.logger.info("雅虎闲置议价：本地队列无待盯价记录")
            return
        page_h = self._page_watch_hours()
        self.logger.info(
            "雅虎闲置议价：开始盯价，watching %s 条（页面盯价 %s 小时 / 超时 %s 小时）",
            len(watching),
            page_h,
            hours,
        )
        total = len(watching)
        for i, rec in enumerate(watching, 1):
            oid = str(rec.get("order_id") or "")
            try:
                use_page = self._use_page_watch(rec)
                self.logger.info(
                    "雅虎闲置议价：盯价 %s/%s %s order=%s",
                    i,
                    total,
                    "打开商品页" if use_page else "商品API",
                    oid,
                )
                self._monitor_one(rec)
            except Exception as e:
                self.logger.error(
                    "雅虎闲置议价：盯价异常 order=%s: %s", oid, e, exc_info=True
                )

    def _monitor_one(self, rec: Dict[str, Any]) -> None:
        if self._use_page_watch(rec):
            self._monitor_one_page(rec)
        else:
            self._monitor_one_api(rec)

    def _monitor_one_page(self, rec: Dict[str, Any]) -> None:
        products = rec.get("products") or []
        product = products[0] if products else {}
        url = str(product.get("url") or "").strip()
        item_id = extract_yahoo_item_id(url)
        bargain_yen = _yen_int(rec.get("bargain_price") or product.get("bargain_price"))
        oid = str(rec.get("order_id") or "")
        if not item_id or not bargain_yen:
            self._consume(
                rec,
                "lost",
                "记录缺少 item_id 或议价金额",
                ["议价记录数据不完整，无法盯价 order=%s" % oid],
            )
            return

        # 议价成交价是登录账号专属价，商品 API 看不到；必须打开页面判断。
        state, page_err = self.yp.inspect_bargain_watch_page(url)
        if page_err or not state:
            self.logger.warning(
                "雅虎闲置议价：打开商品页失败，本轮跳过 order=%s item=%s err=%s",
                oid,
                item_id,
                page_err or "empty",
            )
            return
        got_id = str(state.get("page_item_id") or "").strip()
        if got_id and got_id != item_id:
            self.logger.warning(
                "雅虎闲置议价：页面仍是另一件商品，本轮跳过（不改本单状态） "
                "order=%s expect=%s got=%s",
                oid,
                item_id,
                got_id,
            )
            return

        rec["last_check_at"] = _now_iso()
        rec["last_seen_price"] = state.get("deal_price")
        rec["last_page_accepted"] = bool(state.get("accepted"))
        self.store.upsert(rec)

        if state.get("sold"):
            self._consume(
                rec,
                "lost",
                "商品页显示已售出/不可购",
                ["议价商品已不可售/已售出 item=%s" % item_id, url],
            )
            return

        page_title = _norm_text(state.get("title"))
        if page_title:
            snap = dict(rec.get("item_snapshot") or {})
            if not snap.get("title"):
                snap["item_id"] = item_id
                snap["title"] = page_title
                rec["item_snapshot"] = snap
                self.store.upsert(rec)
            else:
                saved_title = _norm_text(snap.get("title"))
                # 只比主栏 ItemTitle；泛化 h1 会误抓页头/推荐。卖家已同意时不因此停买。
                if (
                    saved_title
                    and page_title
                    and not titles_equivalent(saved_title, page_title)
                    and not state.get("accepted")
                    and (state.get("page_matched") or got_id == item_id)
                ):
                    listed = state.get("deal_price")
                    self.logger.warning(
                        "雅虎闲置议价：标题不一致 order=%s 快照=%s 页面=%s 价=%s",
                        oid,
                        saved_title,
                        page_title,
                        listed,
                    )
                    self._consume(
                        rec,
                        "lost",
                        "商品信息与议价时不一致: 标题不一致",
                        [
                            "议价后卖家可能改了商品（标题不一致），已停止自动购买",
                            "item=%s 页面价=%s 议价=%s" % (item_id, listed, bargain_yen),
                            "快照标题: %s" % saved_title,
                            "页面标题: %s" % page_title,
                            url,
                        ],
                    )
                    return

        listed = state.get("deal_price")
        if listed is None:
            self.logger.warning(
                "雅虎闲置议价：商品页无法读取价格，本轮跳过 order=%s item=%s",
                oid,
                item_id,
            )
            return
        if listed > bargain_yen:
            if state.get("accepted"):
                self.logger.info(
                    "雅虎闲置议价：卖家已同意但专属价 %s > 议价 %s，继续等待 "
                    "order=%s item=%s",
                    listed,
                    bargain_yen,
                    oid,
                    item_id,
                )
            else:
                self.logger.info(
                    "雅虎闲置议价：页面标价 %s > 议价 %s，卖家尚未同意专属价，继续等待 "
                    "order=%s item=%s",
                    listed,
                    bargain_yen,
                    oid,
                    item_id,
                )
            return

        if state.get("accepted"):
            self.logger.info(
                "雅虎闲置议价：卖家已同意专属价 %s <= 议价 %s，开始自动购买 "
                "order=%s buy=%s",
                listed,
                bargain_yen,
                oid,
                state.get("buy_text") or "相談した価格で購入手続きへ",
            )
        else:
            self.logger.info(
                "雅虎闲置议价：页面标价 %s <= 议价 %s，开始自动购买 order=%s",
                listed,
                bargain_yen,
                oid,
            )
        self._try_auto_buy(rec, item_id, url, listed, bargain_yen, oid)

    def _monitor_one_api(self, rec: Dict[str, Any]) -> None:
        """超过页面盯价时长、或提交时无议价窗口：只请求商品 API，不打开页面。"""
        products = rec.get("products") or []
        product = products[0] if products else {}
        url = str(product.get("url") or "").strip()
        item_id = extract_yahoo_item_id(url)
        bargain_yen = _yen_int(rec.get("bargain_price") or product.get("bargain_price"))
        oid = str(rec.get("order_id") or "")
        if not item_id or not bargain_yen:
            self._consume(
                rec,
                "lost",
                "记录缺少 item_id 或议价金额",
                ["议价记录数据不完整，无法盯价 order=%s" % oid],
            )
            return

        data, err = self.yp._fetch_item_status(item_id)
        if err and not data:
            self.logger.warning(
                "雅虎闲置议价：商品 API 失败，本轮跳过 order=%s item=%s err=%s",
                oid,
                item_id,
                err,
            )
            return
        listed = item_listed_price(data)
        rec["last_check_at"] = _now_iso()
        rec["last_seen_price"] = listed
        rec["watch_mode"] = "api"
        if data:
            current_snap = build_item_snapshot(data, item_id)
            saved = dict(rec.get("item_snapshot") or {})
            api_title = _norm_text(current_snap.get("title"))
            if not saved.get("title"):
                rec["item_snapshot"] = current_snap
            elif api_title:
                saved_title = _norm_text(saved.get("title"))
                if saved_title and not titles_equivalent(saved_title, api_title):
                    self._consume(
                        rec,
                        "lost",
                        "商品信息与议价时不一致: 标题不一致",
                        [
                            "议价后卖家可能改了商品（标题不一致），已停止自动购买",
                            "item=%s API价=%s 议价=%s" % (item_id, listed, bargain_yen),
                            "快照标题: %s" % saved_title,
                            "API标题: %s" % api_title,
                            url,
                        ],
                    )
                    return
        self.store.upsert(rec)

        if data and not item_is_on_sale(data):
            self._consume(
                rec,
                "lost",
                "商品 API 显示已售出/不可售",
                [
                    "议价商品已不可售/已售出 item=%s status=%s"
                    % (item_id, _dig(data, "status")),
                    url,
                ],
            )
            return
        if listed is None:
            self.logger.warning(
                "雅虎闲置议价：商品 API 无价格，本轮跳过 order=%s item=%s",
                oid,
                item_id,
            )
            return
        if listed > bargain_yen:
            self.logger.info(
                "雅虎闲置议价：API 标价 %s > 议价 %s，继续等待 order=%s item=%s",
                listed,
                bargain_yen,
                oid,
                item_id,
            )
            return
        self.logger.info(
            "雅虎闲置议价：API 标价 %s <= 议价 %s，开始自动购买 order=%s",
            listed,
            bargain_yen,
            oid,
        )
        self._try_auto_buy(rec, item_id, url, listed, bargain_yen, oid)

    def _try_auto_buy(
        self,
        rec: Dict[str, Any],
        item_id: str,
        url: str,
        listed: int,
        bargain_yen: int,
        oid: str,
    ) -> None:
        from src.utils.dev_test import stop_before_purchase as _dev_stop_buy

        if _dev_stop_buy(self.config):
            self.logger.warning(
                "本地测试：达价且信息一致，按配置不自动购买 order=%s item=%s 现价=%s",
                oid,
                item_id,
                listed,
            )
            return
        order = record_to_order(rec)
        try:
            bought, summary = self._process_order(order)
        except Exception as e:
            bought = False
            summary = {"failure_reason": str(e)}
            self.logger.error("雅虎闲置议价：自动购买异常 order=%s: %s", oid, e, exc_info=True)
        if self._cooldown:
            try:
                self._cooldown()
            except Exception:
                pass
        if bought:
            self._consume(
                rec,
                "ok",
                "议价达成并下单成功 现价=%s" % listed,
                [
                    "议价订单已自动下单成功 order=%s item=%s 成交价=%s 议价=%s"
                    % (oid, item_id, listed, bargain_yen)
                ],
            )
            return
        fail = (summary or {}).get("failure_reason") or "下单未成功"
        self._consume(
            rec,
            "lost",
            "下单未成功: %s" % fail,
            [
                "议价已达价但自动下单失败: %s" % fail,
                "item=%s 现价=%s 议价=%s" % (item_id, listed, bargain_yen),
                url,
            ],
        )

    def fetch_and_submit_new(self) -> None:
        """普通拉单之后：拉议价接口并提交価格の相談。"""
        if not self.enabled_submit():
            return
        self.logger.info("雅虎闲置议价：开始请求 getBargainOrderListSimple")
        try:
            orders = self.fetcher.fetch_bargain_orders()
        except Exception as e:
            self.logger.error("雅虎闲置议价：拉单失败: %s", e, exc_info=True)
            return
        self.logger.info("雅虎闲置议价：接口返回 %s 单", len(orders))

        for order in orders:
            oid = str(order.get("order_id") or "").strip()
            if not oid:
                continue
            existing = self.store.get(oid)
            if existing and str(existing.get("status") or "") not in ("pending_submit",):
                self.logger.info(
                    "雅虎闲置议价：订单 %s 已在本地 status=%s，跳过重复提交",
                    oid,
                    existing.get("status"),
                )
                continue
            rec = existing or self._new_record(order)
            if existing is None:
                self.store.upsert(rec)
                self.logger.info(
                    "雅虎闲置议价：已持久化 Mark/Secret order=%s mark=%s",
                    oid,
                    rec.get("mark"),
                )
            if self._drop_if_prohibited(rec, order):
                continue
            if self._drop_if_same_item_already_active(rec):
                continue
            if existing is not None and self._dev_accept_unverified_submit(rec):
                continue
            self._submit_one(rec)

        # 接口没返回、但本地仍 pending 的，本轮补提交
        for rec in self.store.list_active():
            if str(rec.get("status") or "") != "pending_submit":
                continue
            oid = str(rec.get("order_id") or "")
            if any(str(o.get("order_id") or "") == oid for o in orders):
                continue
            if self._drop_if_prohibited(rec):
                continue
            if self._drop_if_same_item_already_active(rec):
                continue
            if self._dev_accept_unverified_submit(rec):
                continue
            self.logger.info("雅虎闲置议价：补提交本地 pending_submit order=%s", oid)
            self._submit_one(rec)

    def _new_record(self, order: Dict[str, Any]) -> Dict[str, Any]:
        products = list(order.get("products") or [])
        bargain_price = None
        if products:
            bargain_price = products[0].get("bargain_price")
        return {
            "order_id": str(order.get("order_id") or ""),
            "order_no": str(order.get("order_no") or order.get("order_id") or ""),
            "user_id": order.get("user_id"),
            "mark": order.get("mark"),
            "secret": order.get("secret"),
            "service_type_ids": list(order.get("service_type_ids") or []),
            "products": products,
            "order_payload": {
                "order_id": order.get("order_id"),
                "order_no": order.get("order_no"),
                "user_id": order.get("user_id"),
                "mark": order.get("mark"),
                "secret": order.get("secret"),
                "service_type_ids": list(order.get("service_type_ids") or []),
                "products": products,
            },
            "item_snapshot": None,
            "status": "pending_submit",
            "bargain_price": bargain_price,
            "last_seen_price": None,
            "last_check_at": None,
            "bargain_submitted_at": None,
            "fetched_at": _now_iso(),
            "note": "",
            "submit_attempts": 0,
        }

    def _drop_if_prohibited(
        self, rec: Dict[str, Any], order: Optional[Dict[str, Any]] = None
    ) -> bool:
        from src.order.prohibited_item_guard import inspect_order, is_enabled

        if not is_enabled(self.config):
            return False
        hit = inspect_order(order or record_to_order(rec), self.config)
        if not hit:
            return False
        oid = str(rec.get("order_id") or "")
        note = "禁售商品 %s 关键词=%s" % (hit.category, hit.keyword)
        self.logger.warning("雅虎闲置议价：%s order=%s", note, oid)
        self._consume(
            rec,
            "lost",
            note,
            [
                "疑似禁售商品，议价单已停止提交、不再重试",
                "分类：%s" % hit.category,
                "命中关键词：%s（字段 %s）" % (hit.keyword, hit.field),
                "文本：%s" % hit.snippet,
            ],
        )
        return True

    def _drop_if_same_item_already_active(self, rec: Dict[str, Any]) -> bool:
        """同一商品已有 watching/pending 单，不再提交、不再重试。"""
        oid = str(rec.get("order_id") or "")
        item_id = self._record_item_id(rec)
        other = self._active_other_for_item(item_id, oid)
        if not other:
            return False
        other_oid = str(other.get("order_id") or "")
        other_st = str(other.get("status") or "")
        note = "同商品已有议价单 %s（%s），跳过重复提交" % (other_oid, other_st or "active")
        self.logger.warning(
            "雅虎闲置议价：%s order=%s item=%s",
            note,
            oid,
            item_id,
        )
        self._consume(
            rec,
            "lost",
            note,
            [
                "同一商品已在议价队列中，本单停止提交、不再重试",
                "本单 order=%s 已有单 order=%s status=%s item=%s"
                % (oid, other_oid, other_st, item_id),
            ],
        )
        return True

    def _submit_one(self, rec: Dict[str, Any]) -> None:
        oid = str(rec.get("order_id") or "")
        if str(rec.get("watch_mode") or "").strip().lower() == "api":
            if str(rec.get("status") or "") != "watching":
                rec["status"] = "watching"
                self.store.upsert(rec)
            self.logger.info("雅虎闲置议价：已改 API 盯价，跳过页面提交 order=%s", oid)
            return
        products = rec.get("products") or []
        if not products:
            self._consume(rec, "lost", "议价单无商品", ["议价订单无商品 order=%s" % oid])
            return
        product = products[0]
        url = str(product.get("url") or "").strip()
        item_id = extract_yahoo_item_id(url)
        bargain_yen = _yen_int(rec.get("bargain_price") or product.get("bargain_price"))
        if not item_id:
            self._consume(
                rec,
                "lost",
                "无法解析 item id",
                ["议价单无法解析商品 ID url=%s" % url],
            )
            return
        if not bargain_yen:
            self._consume(
                rec,
                "lost",
                "BargainPrice 无效",
                ["议价金额无效 order=%s BargainPrice=%s" % (oid, rec.get("bargain_price"))],
            )
            return
        if self._drop_if_same_item_already_active(rec):
            return

        data, err = self.yp._fetch_item_status(item_id)
        if data:
            rec["item_api_at_submit"] = _jsonable(data)
            rec["item_snapshot"] = build_item_snapshot(data, item_id)
            rec["last_seen_price"] = item_listed_price(data)
            rec["last_check_at"] = _now_iso()
            self.store.upsert(rec)
        if data and not item_is_on_sale(data):
            self._consume(
                rec,
                "lost",
                "提交前商品不可售",
                [
                    "议价提交前商品已不可售 item=%s status=%s"
                    % (item_id, _dig(data, "status")),
                    url,
                ],
            )
            return
        if err:
            self.logger.warning(
                "雅虎闲置议价：提交前商品 API 失败，仍尝试打开页面 order=%s err=%s",
                oid,
                err,
            )

        ok, msg = self.yp.submit_price_consultation(url, bargain_yen, item_id)
        if self._cooldown:
            try:
                self._cooldown()
            except Exception:
                pass
        if not ok:
            if self._should_drop_unsupported_bargain(msg):
                self._switch_to_api_watch(
                    rec,
                    msg,
                    [msg, "item=%s url=%s 议价金额=%s" % (item_id, url, bargain_yen)],
                )
                return
            attempts = int(rec.get("submit_attempts") or 0) + 1
            rec["submit_attempts"] = attempts
            rec["note"] = msg
            rec["last_check_at"] = _now_iso()
            max_n = self._submit_max_attempts()
            if attempts < max_n:
                self.store.upsert(rec)
                self.logger.warning(
                    "雅虎闲置议价：提交未完成，本轮不发飞书，留待下轮重试 "
                    "order=%s attempt=%s/%s %s",
                    oid,
                    attempts,
                    max_n,
                    msg,
                )
                return
            self.logger.warning(
                "雅虎闲置议价：提交失败已达 %s 次，停止重试 order=%s %s",
                max_n,
                oid,
                msg,
            )
            self._consume(
                rec,
                "lost",
                "提交失败已达 %s 次: %s" % (max_n, msg),
                [
                    "议价提交连续 %s 次未进入协商页，已停止重试，请人工看协商是否已发出"
                    % max_n,
                    msg,
                    "item=%s 议价金额=%s url=%s" % (item_id, bargain_yen, url),
                ],
            )
            return

        rec["status"] = "watching"
        rec["watch_mode"] = "page"
        rec["bargain_submitted_at"] = _now_iso()
        rec["note"] = "已提交価格の相談 %s円" % bargain_yen
        self.store.upsert(rec)
        self.logger.info(
            "雅虎闲置议价：提交完成，进入盯价队列 order=%s item=%s yen=%s",
            oid,
            item_id,
            bargain_yen,
        )
