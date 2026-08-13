"""
任务结束后将本批订单汇总写入 is_success.log，便于工作人员处理记录。
"""

import os
from datetime import datetime
from typing import Dict, Any, List


SEP = "—" * 40


def write_is_success_log(config: Dict[str, Any], summaries: List[Dict[str, Any]]) -> None:
    """
    将本批订单汇总追加写入 logs/is_success.log。
    
    Args:
        config: 全局配置（用于取 logging.log_dir）
        summaries: 每个订单的 summary 列表（与 order_processor / payment_handler 返回结构一致）
    """
    if not summaries:
        return
    log_config = config.get("logging", {})
    log_dir = log_config.get("log_dir", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "is_success.log")
    
    lines = [
        "",
        SEP,
        "任务执行完成时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "本批订单数: %s" % len(summaries),
        SEP,
        "",
    ]
    
    for s in summaries:
        order_no = s.get("order_no", "") or "—"
        success = s.get("success", False)
        payment_method = s.get("payment_method", "") or "—"
        failure_reason = (s.get("failure_reason") or "").strip() or "（请手动记录，如：响应状态码 0、Success=false、Data=false 等）"
        check_requested = "已请求" if s.get("check_cart_requested") else "未请求"
        check_response = (s.get("check_cart_response") or "未请求").strip() or "未请求"
        add_requested = "已请求" if s.get("add_no_requested") else "未请求"
        add_response = (s.get("add_no_response") or "未请求").strip() or "未请求"
        update_errors = s.get("update_errors") or []
        
        success_text = "addNoCallbackSimple 及 updateGoodsNoCallback 接口成功"
        if not success:
            success_text = "失败"
        elif update_errors:
            success_text = "addNoCallbackSimple 成功；updateGoodsNoCallback 部分失败（见下方）"
        
        block = [
            "OrderNo：%s" % order_no,
            "订单是否成功：%s" % success_text,
            "支付方式：%s" % payment_method,
            "失败原因：%s" % failure_reason,
        ]
        purchase_nos = s.get("purchase_nos") or []
        if purchase_nos:
            block.append(
                "取引番号：%s"
                % ("、".join(str(x) for x in purchase_nos if str(x).strip()) or "—")
            )
        amount_total = s.get("amount_total")
        if amount_total is not None and str(amount_total).strip() != "":
            block.append("合计金额：%s 円" % amount_total)
        block.extend(
            [
                "关键请求情况：",
                "1.checkCartGoodsSimple 接口【支付前】",
                "   是否请求：%s" % check_requested,
                "   请求结果：%s" % check_response,
                "2.addNoCallbackSimple 接口【支付后】",
                "   是否请求：%s" % add_requested,
                "   请求结果：%s" % add_response,
            ]
        )
        if update_errors:
            block.append("3.updateGoodsNoCallback 部分失败：")
            for err in update_errors:
                block.append("   - %s" % err)
        block.append("")
        lines.extend(block)
    
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")
