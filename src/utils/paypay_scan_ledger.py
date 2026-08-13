# -*- coding: utf-8 -*-
"""
PayPay 半自动扫码支付成功留底（财务对账）。

每笔成功支付追加一行 JSON（JSONL），字段含：
RS 单号、订单 ID、支付时间、金额、骏河屋取引番号（注文番号）、支付方式等。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _resolve_ledger_path(config: Dict[str, Any]) -> str:
    payment = config.get("payment") or {}
    path = (payment.get("paypay_scan_ledger_file") or "").strip() or (
        "data/paypay_scan_ledger.jsonl"
    )
    project_root = Path(__file__).parent.parent.parent
    if not os.path.isabs(path):
        path = str((project_root / path).resolve())
    return path


def append_paypay_scan_record(
    config: Dict[str, Any],
    *,
    order: Dict[str, Any],
    purchase_nos: List[str],
    amount_total: Optional[int] = None,
    amount_goods: Optional[int] = None,
    amount_operate: Optional[int] = None,
    payment_method: str = "paypay",
    add_no_ok: Optional[bool] = None,
    note: str = "",
) -> str:
    """
    追加一条 PayPay 扫码成功对账记录。

    Returns:
        写入的文件绝对路径
    """
    ledger_path = _resolve_ledger_path(config)
    os.makedirs(os.path.dirname(ledger_path) or ".", exist_ok=True)

    order_id = str(order.get("order_id") or "").strip()
    order_no = str(order.get("order_no") or order_id).strip()
    paid_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    record = {
        "paid_at": paid_at,
        "order_no": order_no,
        "order_id": order_id,
        "payment_method": payment_method or "paypay",
        "amount_total": amount_total,
        "amount_goods": amount_goods,
        "amount_operate": amount_operate,
        "purchase_nos": [str(x).strip() for x in (purchase_nos or []) if str(x).strip()],
        "add_no_ok": add_no_ok,
        "note": (note or "").strip(),
        "currency": "JPY",
    }

    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return ledger_path
