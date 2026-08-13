"""
PayPay 扫码订单队列（JSON 文件）。

用途：
- 拉单时发现商品含 tenpo_cd / 第三方店铺（需要 PayPay 扫码）→ 不进入正常自动流程，写入队列文件并发飞书；
- R18 / 预售：不入本队列，仅飞书通知人工；
- 到扫码时间段开始（或人工点击）→ 从队列取出订单并依次执行后续加购/支付流程；
- 为避免重复使用：队列采用「消费即移除」策略（取出处理后不会再次处理）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _resolve_queue_path(config: Dict[str, Any]) -> str:
    payment = config.get("payment") or {}
    path = (payment.get("paypay_queue_file") or "").strip() or "data/paypay_scan_queue.json"
    project_root = Path(__file__).parent.parent.parent
    if not os.path.isabs(path):
        path = str((project_root / path).resolve())
    return path


def enqueue_paypay_order(config: Dict[str, Any], order: Dict[str, Any]) -> None:
    """将订单追加写入 PayPay 队列文件（去重：按 order_id）。"""
    queue_path = _resolve_queue_path(config)
    os.makedirs(os.path.dirname(queue_path), exist_ok=True)
    data = {"items": []}
    if os.path.exists(queue_path):
        try:
            with open(queue_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {"items": []}
        except Exception:
            data = {"items": []}
    items: List[Dict[str, Any]] = data.get("items") or []

    order_id = str(order.get("order_id") or "").strip()
    if not order_id:
        return
    # 去重：同一个 order_id 只保留一条（保留最新）
    items = [it for it in items if str((it.get("order") or {}).get("order_id") or "") != order_id]

    items.append(
        {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "order": order,
        }
    )
    data["items"] = items
    with open(queue_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def consume_all_paypay_orders(config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """
    一次性取出并清空队列（消费即移除，防止重复使用）。

    Returns:
        (orders, removed_count)
    """
    queue_path = _resolve_queue_path(config)
    if not os.path.exists(queue_path):
        return [], 0
    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    items: List[Dict[str, Any]] = data.get("items") or []
    orders = []
    for it in items:
        o = it.get("order")
        if isinstance(o, dict) and o.get("order_id"):
            orders.append(o)
    removed = len(items)

    # 清空队列
    try:
        with open(queue_path, "w", encoding="utf-8") as f:
            json.dump({"items": []}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return orders, removed


def get_paypay_queue_size(config: Dict[str, Any]) -> int:
    queue_path = _resolve_queue_path(config)
    if not os.path.exists(queue_path):
        return 0
    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        items = data.get("items") or []
        return len(items)
    except Exception:
        return 0

