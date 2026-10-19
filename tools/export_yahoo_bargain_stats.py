# -*- coding: utf-8 -*-
"""
雅虎闲置议价统计：读取本地议价记录表，导出运营/技术用 Excel。

用法（在项目根目录 paypay/paypay/paypay）：
  python tools/export_yahoo_bargain_stats.py
  python tools/export_yahoo_bargain_stats.py --open
  python tools/export_yahoo_bargain_stats.py --input data/yahoo_bargain_records.json
  python tools/export_yahoo_bargain_stats.py --output data/议价统计.xlsx

默认读 config.yaml 里 yahoo_fleamarket.bargain_records_file，
否则 data/yahoo_bargain_records.json。只读本地表，不请求 EDI、不改记录。
Excel 不含 secret。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.order.yahoo_bargain_store import (  # noqa: E402
    ACTIVE_STATUSES,
    YahooBargainStore,
)

TZ_CN = timezone(timedelta(hours=8))
_ITEM_PATH_RE = re.compile(r"/item/(?P<id>[^/?#]+)", re.IGNORECASE)


class Pct(float):
    """Excel 百分比单元格（避免普通 float 被当成百分数）。"""


def extract_item_id(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    m = _ITEM_PATH_RE.search(text)
    if m:
        return (m.group("id") or "").strip()
    last = text.rstrip("/").split("/")[-1]
    if re.match(r"^[a-zA-Z0-9_-]+$", last):
        return last
    return ""


def _load_yaml_config() -> Dict[str, Any]:
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        import yaml

        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _patch_yahoo_section(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把 sites[] 里雅虎闲置的覆盖合并进顶层 yahoo_fleamarket。"""
    out = dict(cfg)
    y = dict(out.get("yahoo_fleamarket") or {})
    for site in out.get("sites") or []:
        if not isinstance(site, dict):
            continue
        adapter = str(site.get("adapter") or "").strip().lower()
        if adapter not in ("yahoo_fleamarket", "paypayfleamarket"):
            continue
        patch = site.get("yahoo_fleamarket")
        if isinstance(patch, dict):
            y.update(patch)
        extra = str(site.get("bargain_records_file") or "").strip()
        if extra:
            y["bargain_records_file"] = extra
    out["yahoo_fleamarket"] = y
    return out


def _timeout_hours(cfg: Dict[str, Any]) -> float:
    y = cfg.get("yahoo_fleamarket") or {}
    raw = y.get("bargain_timeout_hours")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
    return 72.0


def resolve_records_path(cfg: Dict[str, Any], input_path: str = "") -> Path:
    if str(input_path or "").strip():
        p = Path(input_path.strip())
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        return p
    store = YahooBargainStore(cfg)
    return Path(store.path)


def load_records(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    if not path.is_file():
        raise FileNotFoundError("找不到议价记录表: %s" % path)
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("议价记录表格式不对（根节点应是对象）: %s" % path)
    orders = data.get("orders") or []
    if not isinstance(orders, list):
        raise ValueError("议价记录表缺少 orders 数组: %s" % path)
    recs = [r for r in orders if isinstance(r, dict)]
    updated = str(data.get("updated_at") or "").strip()
    return updated, recs


def parse_iso(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_CN)
    return dt.astimezone(TZ_CN)


def cn_date(raw: Any) -> str:
    dt = parse_iso(raw)
    return dt.strftime("%Y-%m-%d") if dt else ""


def cn_dt(raw: Any) -> str:
    dt = parse_iso(raw)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def is_purchase_success(rec: Dict[str, Any]) -> bool:
    return str(rec.get("status") or "").strip() == "ok"


def is_bargain_success(rec: Dict[str, Any]) -> bool:
    """
    与此前统计口径一致：系统已按达价启动自动购买（含买失败）。
    标题不一致 / API 已售出 / 超时 / 无效价 / 无窗口 不算议价成功。
    """
    if is_purchase_success(rec):
        return True
    if rec.get("last_page_accepted") is True:
        return True
    note = str(rec.get("note") or "")
    if "议价达成" in note:
        return True
    if note.startswith("下单未成功"):
        return True
    return False


def outcome_label(rec: Dict[str, Any]) -> str:
    status = str(rec.get("status") or "").strip()
    note = str(rec.get("note") or "")
    if status == "ok":
        return "购买成功"
    if note.startswith("下单未成功") or "议价达成" in note:
        return "议价成功但下单失败"
    if "标题不一致" in note:
        return "标题不一致"
    if "已售出" in note or "不可售" in note:
        return "商品已售出/不可售"
    if status == "time_out" or "超过" in note and "未达成议价" in note:
        return "超时未达成"
    if "BargainPrice" in note or "议价金额无效" in note:
        return "BargainPrice无效"
    if "不支持议价" in note or "未找到议价窗口" in note or "価格の相談" in note and "无" in note:
        return "不支持议价/无窗口"
    if "禁售" in note:
        return "禁售商品"
    if status in ACTIVE_STATUSES:
        return "进行中"
    if status == "lost":
        return "其他失败"
    if status:
        return "其他(%s)" % status
    return "未知"


def first_product(rec: Dict[str, Any]) -> Dict[str, Any]:
    products = rec.get("products") or []
    if products and isinstance(products[0], dict):
        return products[0]
    return {}


def product_url(rec: Dict[str, Any]) -> str:
    return str(first_product(rec).get("url") or "").strip()


def product_title(rec: Dict[str, Any]) -> str:
    p = first_product(rec)
    for key in ("title", "name", "goods_name", "item_title"):
        v = str(p.get(key) or "").strip()
        if v:
            return v
    snap = rec.get("item_snapshot")
    if isinstance(snap, dict):
        for key in ("title", "name"):
            v = str(snap.get(key) or "").strip()
            if v:
                return v
    return ""


def item_id_of(rec: Dict[str, Any]) -> str:
    return extract_item_id(product_url(rec))


def watch_start(rec: Dict[str, Any]) -> Optional[datetime]:
    return parse_iso(rec.get("bargain_submitted_at")) or parse_iso(rec.get("fetched_at"))


def timeout_at(rec: Dict[str, Any], hours: float) -> Optional[datetime]:
    start = watch_start(rec)
    if start is None:
        return None
    return start + timedelta(hours=hours)


def yen_text(raw: Any) -> str:
    if raw is None or raw == "":
        return ""
    try:
        return str(int(round(float(str(raw).replace(",", "").strip()))))
    except (TypeError, ValueError):
        return str(raw).strip()


def pct(num: int, den: int) -> Optional[Pct]:
    if den <= 0:
        return None
    return Pct(num / den)


def daily_rows(recs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    unknown: List[Dict[str, Any]] = []
    for rec in recs:
        day = cn_date(rec.get("fetched_at"))
        if day:
            buckets[day].append(rec)
        else:
            unknown.append(rec)
    rows = []
    for day in sorted(buckets):
        group = buckets[day]
        n = len(group)
        b_ok = sum(1 for r in group if is_bargain_success(r))
        p_ok = sum(1 for r in group if is_purchase_success(r))
        timeout_n = sum(1 for r in group if str(r.get("status") or "") == "time_out")
        active_n = sum(1 for r in group if str(r.get("status") or "") in ACTIVE_STATUSES)
        rows.append(
            {
                "date": day,
                "n": n,
                "bargain_ok": b_ok,
                "buy_ok": p_ok,
                "timeout": timeout_n,
                "active": active_n,
                "bargain_rate": pct(b_ok, n),
                "buy_rate": pct(p_ok, n),
            }
        )
    if unknown:
        n = len(unknown)
        b_ok = sum(1 for r in unknown if is_bargain_success(r))
        p_ok = sum(1 for r in unknown if is_purchase_success(r))
        rows.append(
            {
                "date": "(无fetched_at)",
                "n": n,
                "bargain_ok": b_ok,
                "buy_ok": p_ok,
                "timeout": sum(1 for r in unknown if str(r.get("status") or "") == "time_out"),
                "active": sum(
                    1 for r in unknown if str(r.get("status") or "") in ACTIVE_STATUSES
                ),
                "bargain_rate": pct(b_ok, n),
                "buy_rate": pct(p_ok, n),
            }
        )
    return rows


def detail_row(rec: Dict[str, Any], hours: float) -> List[Any]:
    status = str(rec.get("status") or "").strip()
    end = timeout_at(rec, hours) if status in ACTIVE_STATUSES else None
    remaining = ""
    if end is not None:
        now = datetime.now(TZ_CN)
        mins = (end - now).total_seconds() / 60.0
        remaining = "%.1f小时" % (mins / 60.0)
    return [
        str(rec.get("order_id") or ""),
        str(rec.get("order_no") or ""),
        str(rec.get("mark") or ""),
        status,
        outcome_label(rec),
        "是" if is_bargain_success(rec) else "否",
        "是" if is_purchase_success(rec) else "否",
        cn_dt(rec.get("fetched_at")),
        cn_dt(rec.get("bargain_submitted_at")),
        cn_dt(rec.get("consumed_at")),
        cn_dt(rec.get("last_check_at")),
        yen_text(rec.get("bargain_price")),
        yen_text(rec.get("last_seen_price")),
        "是" if rec.get("last_page_accepted") is True else "否",
        str(rec.get("watch_mode") or ""),
        rec.get("submit_attempts") if rec.get("submit_attempts") not in (None, "") else "",
        item_id_of(rec),
        product_title(rec),
        product_url(rec),
        remaining,
        cn_dt(end.isoformat()) if end else "",
        str(rec.get("note") or ""),
    ]


# ---------- 最小 xlsx 写入（无第三方依赖） ----------

def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_ref(row: int, col: int) -> str:
    return "%s%s" % (_col_letter(col), row)


class _XlsxBook:
    HEADER = 1
    NORMAL = 2
    TOTAL = 3
    INT = 4
    PCT = 5
    WRAP = 6

    def __init__(self) -> None:
        self._sheets: List[Dict[str, Any]] = []
        self._strings: List[str] = []
        self._str_index: Dict[str, int] = {}

    def _sid(self, text: str) -> int:
        if text not in self._str_index:
            self._str_index[text] = len(self._strings)
            self._strings.append(text)
        return self._str_index[text]

    def add_sheet(
        self,
        name: str,
        rows: Sequence[Sequence[Any]],
        *,
        col_widths: Optional[Sequence[float]] = None,
        header: bool = True,
        freeze_header: bool = True,
        autofilter: bool = True,
        wrap_last: bool = False,
    ) -> None:
        self._sheets.append(
            {
                "name": name[:31],
                "rows": rows,
                "col_widths": list(col_widths or []),
                "header": header,
                "freeze_header": freeze_header,
                "autofilter": autofilter and header and bool(rows),
                "wrap_last": wrap_last,
            }
        )

    def _style_for(self, value: Any, ridx: int, cidx: int, ncols: int, spec: Dict[str, Any]) -> int:
        if spec["header"] and ridx == 0:
            return self.HEADER
        if spec["wrap_last"] and cidx == ncols - 1 and isinstance(value, str):
            return self.WRAP
        if isinstance(value, Pct):
            return self.PCT
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return self.INT
        if spec["header"] and ridx > 0:
            first = spec["rows"][ridx][0] if spec["rows"][ridx] else ""
            if first == "合计":
                return self.TOTAL
        return self.NORMAL

    def _sheet_xml(self, spec: Dict[str, Any], selected: bool) -> str:
        rows = spec["rows"]
        ncols = max((len(r) for r in rows), default=1)
        parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        ]
        if spec["freeze_header"] and spec["header"]:
            sel = "true" if selected else "false"
            parts.append(
                "<sheetViews><sheetView tabSelected=\"%s\" workbookViewId=\"0\">"
                '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                "</sheetView></sheetViews>" % sel
            )
        widths = spec["col_widths"]
        if widths:
            parts.append("<cols>")
            for i, w in enumerate(widths, start=1):
                parts.append(
                    '<col min="%s" max="%s" width="%s" customWidth="1"/>' % (i, i, w)
                )
            parts.append("</cols>")
        parts.append("<sheetData>")
        for ridx, row in enumerate(rows):
            rr = ridx + 1
            parts.append('<row r="%s">' % rr)
            for cidx in range(ncols):
                value = row[cidx] if cidx < len(row) else None
                style = self._style_for(value, ridx, cidx, ncols, spec)
                ref = _cell_ref(rr, cidx + 1)
                if value is None or value == "":
                    parts.append('<c r="%s" s="%s"/>' % (ref, style))
                    continue
                if isinstance(value, bool):
                    sid = self._sid("是" if value else "否")
                    parts.append('<c r="%s" s="%s" t="s"><v>%s</v></c>' % (ref, style, sid))
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    if isinstance(value, Pct):
                        parts.append(
                            '<c r="%s" s="%s" t="n"><v>%.6f</v></c>' % (ref, style, float(value))
                        )
                    elif isinstance(value, float):
                        parts.append(
                            '<c r="%s" s="%s" t="n"><v>%s</v></c>' % (ref, style, value)
                        )
                    else:
                        parts.append(
                            '<c r="%s" s="%s" t="n"><v>%s</v></c>' % (ref, style, value)
                        )
                else:
                    sid = self._sid(str(value))
                    parts.append('<c r="%s" s="%s" t="s"><v>%s</v></c>' % (ref, style, sid))
            parts.append("</row>")
        parts.append("</sheetData>")
        if spec["autofilter"] and rows:
            last = _cell_ref(len(rows), ncols)
            parts.append('<autoFilter ref="A1:%s"/>' % last)
        parts.append("</worksheet>")
        return "".join(parts)

    def dumps(self) -> bytes:
        buf = BytesIO()
        n_sheets = len(self._sheets)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            overrides = [
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
                '<Override PartName="/xl/styles.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
                '<Override PartName="/xl/sharedStrings.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
            ]
            for i in range(n_sheets):
                overrides.append(
                    '<Override PartName="/xl/worksheets/sheet%s.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    % (i + 1)
                )
            z.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                + "".join(overrides)
                + "</Types>",
            )
            z.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                "</Relationships>",
            )
            sheet_rels = []
            for i, spec in enumerate(self._sheets, start=1):
                sheet_rels.append(
                    '<Relationship Id="rId%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%s.xml"/>'
                    % (i, i)
                )
            sheet_rels.append(
                '<Relationship Id="rId%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                % (n_sheets + 1)
            )
            sheet_rels.append(
                '<Relationship Id="rId%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
                % (n_sheets + 2)
            )
            z.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(sheet_rels)
                + "</Relationships>",
            )
            sheets_xml = []
            for i, spec in enumerate(self._sheets, start=1):
                name = escape(spec["name"])
                sheets_xml.append(
                    '<sheet name="%s" sheetId="%s" r:id="rId%s"/>' % (name, i, i)
                )
            z.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                "<sheets>" + "".join(sheets_xml) + "</sheets></workbook>",
            )
            z.writestr("xl/styles.xml", _STYLES_XML)
            sheet_bodies = [
                self._sheet_xml(spec, selected=(i == 0))
                for i, spec in enumerate(self._sheets)
            ]
            sst_items = []
            for s in self._strings:
                space = ' xml:space="preserve"' if s != s.strip() else ""
                sst_items.append("<si><t%s>%s</t></si>" % (space, escape(s)))
            z.writestr(
                "xl/sharedStrings.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                ' count="%s" uniqueCount="%s">%s</sst>'
                % (len(self._strings), len(self._strings), "".join(sst_items)),
            )
            for i, body in enumerate(sheet_bodies):
                z.writestr("xl/worksheets/sheet%s.xml" % (i + 1), body)
        return buf.getvalue()


_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1">
    <numFmt numFmtId="164" formatCode="0.0%"/>
  </numFmts>
  <fonts count="3">
    <font><sz val="11"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><name val="Microsoft YaHei"/><color rgb="FFFFFFFF"/></font>
    <font><b/><sz val="11"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E79"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFD9D9D9"/></left>
      <right style="thin"><color rgb="FFD9D9D9"/></right>
      <top style="thin"><color rgb="FFD9D9D9"/></top>
      <bottom style="thin"><color rgb="FFD9D9D9"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="7">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">
      <alignment vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment vertical="center"/>
    </xf>
    <xf numFmtId="1" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center"/>
    </xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">
      <alignment vertical="top" wrapText="1"/>
    </xf>
  </cellXfs>
</styleSheet>
"""


def build_workbook(
    recs: Sequence[Dict[str, Any]],
    *,
    source_path: Path,
    updated_at: str,
    timeout_hours: float,
) -> bytes:
    now = datetime.now(TZ_CN)
    n = len(recs)
    bargain_ok = sum(1 for r in recs if is_bargain_success(r))
    buy_ok = sum(1 for r in recs if is_purchase_success(r))
    status_c = Counter(str(r.get("status") or "").strip() or "(空)" for r in recs)
    outcome_c = Counter(outcome_label(r) for r in recs)
    days = daily_rows(recs)

    daily_table: List[List[Any]] = [
        [
            "日期",
            "议价笔数",
            "议价成功笔数",
            "购买成功笔数",
            "超时笔数",
            "进行中笔数",
            "议价成功率",
            "购买成功率",
        ]
    ]
    for row in days:
        daily_table.append(
            [
                row["date"],
                row["n"],
                row["bargain_ok"],
                row["buy_ok"],
                row["timeout"],
                row["active"],
                row["bargain_rate"],
                row["buy_rate"],
            ]
        )
    daily_table.append(
        [
            "合计",
            n,
            bargain_ok,
            buy_ok,
            sum(1 for r in recs if str(r.get("status") or "") == "time_out"),
            sum(1 for r in recs if str(r.get("status") or "") in ACTIVE_STATUSES),
            pct(bargain_ok, n),
            pct(buy_ok, n),
        ]
    )

    overview: List[List[Any]] = [
        ["项", "值"],
        ["导出时间", now.strftime("%Y-%m-%d %H:%M:%S %z")],
        ["记录表路径", str(source_path)],
        ["记录表 updated_at", updated_at or "(无)"],
        ["盯价超时（小时）", timeout_hours],
        ["议价笔数（合计）", n],
        ["议价成功笔数", bargain_ok],
        ["购买成功笔数", buy_ok],
        ["议价成功率", pct(bargain_ok, n)],
        ["购买成功率", pct(buy_ok, n)],
        ["", ""],
        ["当前状态", "笔数"],
    ]
    for k, v in sorted(status_c.items()):
        overview.append([k, v])
    overview.append(["", ""])
    overview.append(["结果分类", "笔数"])
    for k, v in outcome_c.most_common():
        overview.append([k, v])

    active = [r for r in recs if str(r.get("status") or "") in ACTIVE_STATUSES]
    active.sort(key=lambda r: watch_start(r) or datetime.max.replace(tzinfo=TZ_CN))
    active_header = [
        "订单ID",
        "订单号",
        "Mark",
        "状态",
        "结果分类",
        "议价成功",
        "购买成功",
        "进入队列时间",
        "提交议价时间",
        "终态时间",
        "最近检查",
        "议价金额",
        "最近看到的价",
        "页面已达价",
        "盯价方式",
        "提交次数",
        "商品ID",
        "商品标题",
        "商品链接",
        "距超时剩余",
        "预计超时时间",
        "备注",
    ]
    active_table: List[List[Any]] = [active_header]
    for rec in active:
        active_table.append(detail_row(rec, timeout_hours))
    if len(active_table) == 1:
        active_table.append(["(当前没有进行中的议价单)"] + [""] * (len(active_header) - 1))

    details: List[List[Any]] = [active_header]
    ordered = sorted(
        recs,
        key=lambda r: parse_iso(r.get("fetched_at")) or datetime.min.replace(tzinfo=TZ_CN),
    )
    for rec in ordered:
        details.append(detail_row(rec, timeout_hours))

    glossary = [
        ["口径说明（给运营/技术）"],
        [""],
        ["字段", "含义"],
        ["议价笔数", "当天进入本地议价记录表的订单数。按 fetched_at 的北京时间日期归天。"],
        [
            "议价成功笔数",
            "卖家降到议价金额及以下，系统已启动自动购买（含买失败）。"
            "判定：status=ok，或 last_page_accepted=true，或备注含「议价达成」，或备注以「下单未成功」开头。",
        ],
        [
            "购买成功笔数",
            "status=ok，即议价达价后自动下单成功。",
        ],
        [
            "不算议价成功",
            "标题不一致、商品 API 显示已售出/不可售、72 小时超时、BargainPrice 无效、找不到议价窗口/不支持议价。",
        ],
        [
            "超时笔数",
            "status=time_out。当前逻辑只会对「进行中」记录超时后 cancelOrderSimple；已是 ok/lost/time_out 的历史单不会再退款。",
        ],
        [
            "进行中",
            "status 为 pending_submit 或 watching。这类单仍可能在超时后自动取消退款；人工已买但记录仍是 watching 时需留意。",
        ],
        ["本表来源", "只读本地 JSON，不改记录、不请求 EDI、不导出 secret。"],
    ]

    detail_widths = [
        12, 14, 14, 14, 20, 10, 10, 20, 20, 20, 20, 12, 14, 12, 10, 10, 18, 28, 36, 14, 20, 48,
    ]
    book = _XlsxBook()
    book.add_sheet(
        "按日汇总",
        daily_table,
        col_widths=[14, 12, 14, 14, 12, 12, 12, 12],
    )
    book.add_sheet("总览", overview, col_widths=[22, 70], freeze_header=True)
    book.add_sheet(
        "进行中",
        active_table,
        col_widths=detail_widths,
        wrap_last=True,
    )
    book.add_sheet(
        "明细",
        details,
        col_widths=detail_widths,
        wrap_last=True,
    )
    book.add_sheet("口径说明", glossary, col_widths=[18, 90], autofilter=False)
    return book.dumps()


def print_daily_preview(days: Sequence[Dict[str, Any]], n: int, bargain_ok: int, buy_ok: int) -> None:
    print("")
    print("%-12s %8s %10s %10s" % ("日期", "议价", "议价成功", "购买成功"))
    print("-" * 46)
    for row in days:
        print(
            "%-12s %8s %10s %10s"
            % (row["date"], row["n"], row["bargain_ok"], row["buy_ok"])
        )
    print("-" * 46)
    print("%-12s %8s %10s %10s" % ("合计", n, bargain_ok, buy_ok))
    print("")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="导出雅虎闲置议价统计 Excel")
    p.add_argument("--input", "-i", default="", help="议价记录 JSON 路径（默认读配置/data 下的表）")
    p.add_argument("--output", "-o", default="", help="输出 xlsx 路径")
    p.add_argument("--dev-test", action="store_true", help="读本地测试议价表（dev_test）")
    p.add_argument("--open", action="store_true", help="导出后用系统默认程序打开")
    p.add_argument("--no-open", action="store_true", help="不打开（默认也不打开，仅显式覆盖 --open）")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    cfg = _patch_yahoo_section(_load_yaml_config())
    if args.dev_test:
        cfg.setdefault("dev_test", {})
        if isinstance(cfg["dev_test"], dict):
            cfg["dev_test"]["enabled"] = True
    src = resolve_records_path(cfg, args.input)
    updated_at, recs = load_records(src)
    hours = _timeout_hours(cfg)

    stamp = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    if str(args.output or "").strip():
        out = Path(args.output.strip())
        if not out.is_absolute():
            out = (ROOT / out).resolve()
    else:
        out = (ROOT / "data" / ("yahoo_bargain_stats_%s.xlsx" % stamp)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = build_workbook(
        recs, source_path=src, updated_at=updated_at, timeout_hours=hours
    )
    out.write_bytes(payload)

    n = len(recs)
    bargain_ok = sum(1 for r in recs if is_bargain_success(r))
    buy_ok = sum(1 for r in recs if is_purchase_success(r))
    print("已读取议价记录: %s" % src)
    print("updated_at: %s" % (updated_at or "(无)"))
    print("共 %s 笔  议价成功 %s  购买成功 %s" % (n, bargain_ok, buy_ok))
    print_daily_preview(daily_rows(recs), n, bargain_ok, buy_ok)
    print("Excel: %s" % out)

    if args.open and not args.no_open:
        try:
            os.startfile(str(out))  # type: ignore[attr-defined]
        except Exception as e:
            print("已导出，但自动打开失败: %s" % e)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        print(
            "请把 yahoo_bargain_records.json 放到 data/，或用 --input 指定路径。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    except Exception as e:
        print("导出失败: %s" % e, file=sys.stderr)
        raise SystemExit(1)
