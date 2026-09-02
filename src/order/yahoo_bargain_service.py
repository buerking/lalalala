# -*- coding: utf-8 -*-
"""
雅虎闲置议价：
- 每轮定时任务开头盯本地队列（72h / 在售 / 价格 / 商品信息）
- 普通拉单结束后拉 getBargainOrderListSimple，提交「価格の相談」
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.notification.feishu_notifier import FeishuNotifier
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
        hours = self.y_cfg.get("bargain_timeout_hours")
        if hours is not None and str(hours).strip() != "":
            try:
                return max(1.0, float(hours))
            except (TypeError, ValueError):
                pass
        days = self.y_cfg.get("bargain_max_age_days")
        try:
            return max(1.0, float(days) * 24.0)
        except (TypeError, ValueError):
            return 72.0

    def _is_timeout(self, rec: Dict[str, Any]) -> bool:
        hours = self._timeout_hours()
        start = _parse_iso(rec.get("bargain_submitted_at")) or _parse_iso(
            rec.get("fetched_at")
        )
        if start is None:
            return False
        now = datetime.now().astimezone()
        if start.tzinfo is None:
            start = start.replace(tzinfo=now.tzinfo)
        return now - start >= timedelta(hours=hours)

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
            self._notify(rec, messages, "雅虎闲置议价 %s" % status)

    def monitor_and_purchase(self) -> None:
        """每轮定时任务最前面：处理本地已提交的议价单。"""
        if not self.enabled_monitor():
            return
        actives = self.store.list_active()
        if not actives:
            self.logger.info("雅虎闲置议价：本地队列无待处理记录")
            return
        hours = self._timeout_hours()
        self.logger.info(
            "雅虎闲置议价：开始盯价，本地活跃 %s 条（超时 %s 小时）",
            len(actives),
            hours,
        )
        for rec in actives:
            oid = str(rec.get("order_id") or "")
            st = str(rec.get("status") or "")
            try:
                if self._is_timeout(rec):
                    self._consume(
                        rec,
                        "time_out",
                        "超过 %s 小时未达成议价" % hours,
                        [
                            "议价超时（%s 小时），请人工处理" % hours,
                            "订单号 %s item 议价金额 %s"
                            % (rec.get("order_no") or oid, rec.get("bargain_price")),
                        ],
                    )
                    continue
                if st != "watching":
                    continue
                self._monitor_one(rec)
            except Exception as e:
                self.logger.error(
                    "雅虎闲置议价：盯价异常 order=%s: %s", oid, e, exc_info=True
                )

    def _monitor_one(self, rec: Dict[str, Any]) -> None:
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
        if err or not data:
            self.logger.warning(
                "雅虎闲置议价：商品 API 失败，本轮跳过 order=%s item=%s err=%s",
                oid,
                item_id,
                err,
            )
            return
        rec["last_check_at"] = _now_iso()
        rec["last_seen_price"] = item_listed_price(data)
        current_snap = build_item_snapshot(data, item_id)
        self.store.upsert(rec)

        if not item_is_on_sale(data):
            self._consume(
                rec,
                "lost",
                "商品不可售 status=%s" % (_dig(data, "status") or ""),
                [
                    "议价商品已不可售/已售出 item=%s status=%s"
                    % (item_id, _dig(data, "status")),
                    url,
                ],
            )
            return

        if not rec.get("item_snapshot"):
            rec["item_snapshot"] = current_snap
            self.store.upsert(rec)
            self.logger.warning(
                "雅虎闲置议价：记录无商品快照，已补写，本轮不自动购买 order=%s",
                oid,
            )
            return

        listed = item_listed_price(data)
        if listed is None:
            self.logger.warning(
                "雅虎闲置议价：无法读取现价，本轮跳过 order=%s item=%s", oid, item_id
            )
            return
        if listed > bargain_yen:
            self.logger.info(
                "雅虎闲置议价：现价 %s > 议价 %s，继续等待 order=%s item=%s",
                listed,
                bargain_yen,
                oid,
                item_id,
            )
            return

        ok_match, reason = snapshots_match(rec.get("item_snapshot"), current_snap)
        if not ok_match:
            self._consume(
                rec,
                "lost",
                "商品信息与议价时不一致: %s" % reason,
                [
                    "议价后卖家可能改了商品（%s），已停止自动购买" % reason,
                    "item=%s 现价=%s 议价=%s" % (item_id, listed, bargain_yen),
                    url,
                ],
            )
            return

        self.logger.info(
            "雅虎闲置议价：现价 %s <= 议价 %s 且信息一致，开始自动购买 order=%s",
            listed,
            bargain_yen,
            oid,
        )
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
            self._submit_one(rec)

        # 接口没返回、但本地仍 pending 的，本轮补提交
        for rec in self.store.list_active():
            if str(rec.get("status") or "") != "pending_submit":
                continue
            oid = str(rec.get("order_id") or "")
            if any(str(o.get("order_id") or "") == oid for o in orders):
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
        }

    def _submit_one(self, rec: Dict[str, Any]) -> None:
        oid = str(rec.get("order_id") or "")
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
            if "未找到价格相談" in msg or "未找到议价窗口" in msg:
                self._consume(rec, "lost", msg, [msg, "item=%s url=%s" % (item_id, url)])
                return
            self.logger.warning("雅虎闲置议价：提交未完成，留待下轮重试 order=%s %s", oid, msg)
            rec["note"] = msg
            self.store.upsert(rec)
            try:
                self.feishu.notify_order_issue(
                    oid,
                    [msg, "item=%s 议价金额=%s" % (item_id, bargain_yen)],
                    user_id=rec.get("user_id"),
                    extra="雅虎闲置议价提交失败，将在后续轮次重试",
                )
            except Exception:
                pass
            return

        rec["status"] = "watching"
        rec["bargain_submitted_at"] = _now_iso()
        rec["note"] = "已提交価格の相談 %s円" % bargain_yen
        self.store.upsert(rec)
        self.logger.info(
            "雅虎闲置议价：提交完成，进入盯价队列 order=%s item=%s yen=%s",
            oid,
            item_id,
            bargain_yen,
        )
