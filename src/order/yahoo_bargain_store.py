# -*- coding: utf-8 -*-
"""雅虎闲置议价队列：本地 JSON 持久化 Mark/Secret，重启后可继续盯价。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ACTIVE_STATUSES = ("pending_submit", "watching")
TERMINAL_STATUSES = ("ok", "lost", "time_out")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class YahooBargainStore:
    def __init__(self, config: Dict[str, Any]):
        y_cfg = config.get("yahoo_fleamarket") or {}
        path = (
            (y_cfg.get("bargain_records_file") or "").strip()
            or "data/yahoo_bargain_records.json"
        )
        project_root = Path(__file__).resolve().parent.parent.parent
        if not os.path.isabs(path):
            path = str((project_root / path).resolve())
        self.path = path
        self._lock = threading.Lock()

    def _empty(self) -> Dict[str, Any]:
        return {"updated_at": "", "orders": []}

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            return self._empty()
        if not isinstance(data.get("orders"), list):
            data["orders"] = []
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        data["updated_at"] = _now_iso()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, order_id: str) -> Optional[Dict[str, Any]]:
        oid = str(order_id or "").strip()
        if not oid:
            return None
        with self._lock:
            for rec in self.load().get("orders") or []:
                if isinstance(rec, dict) and str(rec.get("order_id") or "") == oid:
                    return rec
        return None

    def upsert(self, record: Dict[str, Any]) -> None:
        oid = str(record.get("order_id") or "").strip()
        if not oid:
            return
        with self._lock:
            data = self.load()
            orders: List[Dict[str, Any]] = data.get("orders") or []
            found = False
            for i, rec in enumerate(orders):
                if isinstance(rec, dict) and str(rec.get("order_id") or "") == oid:
                    orders[i] = record
                    found = True
                    break
            if not found:
                orders.append(record)
            data["orders"] = orders
            self._save(data)

    def list_active(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for rec in self.load().get("orders") or []:
                if isinstance(rec, dict) and str(rec.get("status") or "") in ACTIVE_STATUSES:
                    out.append(rec)
            return out

    def consume(self, order_id: str, status: str, note: str = "") -> Optional[Dict[str, Any]]:
        """标记终态并保留记录（不再用 Mark/Secret）。"""
        oid = str(order_id or "").strip()
        st = str(status or "").strip()
        if not oid or st not in TERMINAL_STATUSES:
            return None
        with self._lock:
            data = self.load()
            updated = None
            for rec in data.get("orders") or []:
                if isinstance(rec, dict) and str(rec.get("order_id") or "") == oid:
                    rec["status"] = st
                    rec["consumed_at"] = _now_iso()
                    if note:
                        rec["note"] = note
                    updated = rec
                    break
            if updated is not None:
                self._save(data)
            return updated
