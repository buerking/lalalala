# -*- coding: utf-8 -*-
"""
雅虎闲置 · 议价订单拉取（单文件脚本）

用途：
  调用 getBargainOrderListSimple，将符合条件的议价单写入本地 JSON，
  供后续盯价 / 申请砍价 / 下单逻辑使用；稳定后可并入 GUI。

用法（在项目 paypay/paypay 目录下）：
  python tools/fetch_yahoo_bargain_orders.py
  python tools/fetch_yahoo_bargain_orders.py --page-size 50
  python tools/fetch_yahoo_bargain_orders.py --order-id 16620

签名：复用 src.utils.sign_generator.SignGenerator（全局 order_api.secret）。
PcMark：固定 paypayfleamarket；不传 GroupIds。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.sign_generator import SignGenerator  # noqa: E402

DEFAULT_URL = (
    "https://edi.jpgoodbuy.com/service.php?func=getBargainOrderListSimple"
)
DEFAULT_PC_MARK = "paypayfleamarket"
DEFAULT_OUT = ROOT / "data" / "yahoo_bargain_orders.json"


def _load_order_api_from_config() -> Dict[str, str]:
    """
    读取 config.yaml 的 order_api.secret / get_bargain_order_list_url。
    不依赖 PyYAML（环境可能未安装），用简单行解析。
    """
    cfg_path = ROOT / "config.yaml"
    out: Dict[str, str] = {}
    if not cfg_path.is_file():
        return out
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except Exception:
        return out
    in_order_api = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.strip() == "order_api:":
            in_order_api = True
            continue
        if in_order_api:
            # 下一顶级键（无缩进）结束 order_api
            if line[0] not in (" ", "\t") and line.rstrip().endswith(":"):
                break
            s = line.strip()
            if s.startswith("secret:"):
                out["secret"] = s.split(":", 1)[1].strip().strip("'\"")
            elif s.startswith("get_bargain_order_list_url:"):
                out["get_bargain_order_list_url"] = s.split(":", 1)[1].strip().strip("'\"")
    return out


def _resolve_secret_and_url() -> Tuple[str, str]:
    api = _load_order_api_from_config()
    secret = str(api.get("secret") or "").strip()
    if not secret:
        raise SystemExit("未找到 order_api.secret，请检查 config.yaml")
    url = str(api.get("get_bargain_order_list_url") or "").strip() or DEFAULT_URL
    return secret, url


def _post_with_curl(url: str, body: Dict[str, str], timeout: int = 30) -> Tuple[int, str]:
    pairs = [(k, "" if v is None else str(v)) for k, v in body.items()]
    form_str = urlencode(pairs, doseq=True, encoding="utf-8")
    cmd = [
        "curl",
        "-s",
        "-w",
        "\n%{http_code}",
        "-X",
        "POST",
        "--data",
        form_str,
        "--connect-timeout",
        "10",
        "--max-time",
        str(timeout),
        url,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout + 5,
        encoding="utf-8",
    )
    out = (result.stdout or "").strip()
    lines = out.split("\n")
    if lines and lines[-1].isdigit():
        return int(lines[-1]), "\n".join(lines[:-1]).strip()
    return 0, out or (result.stderr or "")


def fetch_bargain_orders(
    *,
    secret: str,
    url: str,
    pc_mark: str = DEFAULT_PC_MARK,
    page_size: int = 20,
    order_id: str = "",
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    拉取议价订单列表。
    空 OrderId 不参与签名（与 getOrderListSimple 一致）。
    """
    post_body: Dict[str, str] = {
        "OrderId": str(order_id or ""),
        "PcMark": str(pc_mark),
        "PageSize": str(page_size),
    }
    sign_params: Dict[str, Any] = dict(post_body)
    if sign_params.get("OrderId") == "":
        sign_params.pop("OrderId", None)

    sign = SignGenerator(secret).generate_sign(sign_params)
    body = dict(post_body)
    body["Sign"] = sign

    print("[议价拉单] URL:", url)
    print("[议价拉单] PcMark:", pc_mark, "PageSize:", page_size, "OrderId:", repr(order_id))
    print("[议价拉单] sign_params:", sign_params)
    print("[议价拉单] Sign:", sign)

    code, text = _post_with_curl(url, body, timeout=timeout)
    print("[议价拉单] HTTP:", code)
    if code != 200:
        raise RuntimeError("HTTP %s: %s" % (code, (text or "")[:500]))
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise RuntimeError("响应非 JSON: %s | %s" % (e, (text or "")[:300])) from e
    return data


def _parse_orders(api_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not api_data.get("Success"):
        msg = api_data.get("Message") or "Success=false"
        raise RuntimeError("接口失败: %s (ErrorCode=%s)" % (msg, api_data.get("ErrorCode")))

    raw = api_data.get("Data")
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("无法识别的 Data 类型: %s" % type(raw))

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        products = []
        for g in item.get("List") or []:
            if not isinstance(g, dict):
                continue
            products.append(
                {
                    "goods_id": str(g.get("GoodsId") or "").strip(),
                    "goods_no": str(g.get("GoodsNo") or "").strip(),
                    "url": str(g.get("GoodsUrl") or "").strip(),
                    "quantity": int(g.get("GoodsNumber") or 1),
                    "goods_price": float(g.get("GoodsPrice") or 0),
                    "bargain_price": float(g.get("BargainPrice") or 0),
                }
            )
        out.append(
            {
                "order_id": str(item.get("OrderId") or "").strip(),
                "order_no": str(item.get("OrderNo") or "").strip(),
                "user_id": str(item.get("UserId") if item.get("UserId") is not None else ""),
                "mark": item.get("Mark"),
                "secret": item.get("Secret"),
                "service_type_ids": list(item.get("ServiceTypeIds") or []),
                "products": products,
                "status": "watching",
                "last_seen_price": None,
                "last_check_at": None,
                "bargain_submitted_at": None,
                "fetched_at": now,
                "note": "",
            }
        )
    return out


def _merge_into_store(path: Path, new_orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    按 order_id 合并：已有 done/buying 等终态尽量保留状态字段，刷新商品与密钥。
    """
    store: Dict[str, Any] = {"updated_at": "", "orders": []}
    if path.is_file():
        try:
            store = json.loads(path.read_text(encoding="utf-8")) or store
        except Exception:
            store = {"updated_at": "", "orders": []}
    if not isinstance(store.get("orders"), list):
        store["orders"] = []

    by_id: Dict[str, Dict[str, Any]] = {}
    for o in store["orders"]:
        if isinstance(o, dict) and o.get("order_id"):
            by_id[str(o["order_id"])] = o

    preserve_keys = (
        "status",
        "last_seen_price",
        "last_check_at",
        "bargain_submitted_at",
        "note",
    )
    for o in new_orders:
        oid = str(o.get("order_id") or "")
        if not oid:
            continue
        if oid in by_id:
            old = by_id[oid]
            merged = dict(o)
            for k in preserve_keys:
                if old.get(k) not in (None, ""):
                    merged[k] = old.get(k)
            # 已完成的单不强制改回 watching
            if str(old.get("status") or "") in ("done", "buying", "failed"):
                merged["status"] = old.get("status")
            by_id[oid] = merged
        else:
            by_id[oid] = o

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    store["updated_at"] = now
    store["orders"] = list(by_id.values())
    return store


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="拉取雅虎闲置议价订单并写入本地 JSON")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--order-id", default="", help="指定代购订单 ID；空则拉列表")
    parser.add_argument("--pc-mark", default=DEFAULT_PC_MARK)
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="输出 JSON 路径（默认 data/yahoo_bargain_orders.json）",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="不合并已有文件，直接覆盖为本次结果",
    )
    args = parser.parse_args(argv)

    secret, url = _resolve_secret_and_url()

    api_data = fetch_bargain_orders(
        secret=secret,
        url=url,
        pc_mark=args.pc_mark,
        page_size=args.page_size,
        order_id=args.order_id,
        timeout=args.timeout,
    )
    print("[议价拉单] 原始响应:")
    print(json.dumps(api_data, ensure_ascii=False, indent=2))

    orders = _parse_orders(api_data)
    print("[议价拉单] 解析订单数:", len(orders))

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = (ROOT / out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.no_merge:
        store = {
            "updated_at": datetime.now(timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds"),
            "orders": orders,
        }
    else:
        store = _merge_into_store(out_path, orders)

    out_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[议价拉单] 已写入:", out_path)
    print("[议价拉单] 文件内订单总数:", len(store.get("orders") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
