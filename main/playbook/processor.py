# -*- coding: utf-8 -*-
"""
按接口流程下单。接口是里程碑，页面可以有多页：

1. 商品页：价格 / 库存 —— 不过直接飞书
2. 加入购物车（或无车直接买）→ addedCartCallbackSimple
3. checkCartGoodsSimple：进入后可走多页确认，金额在哪一页出现就收下
4. 确认支付：多页跳转，点完最后一页后校验成功页
5. 注文番号（同样每页都试）→ addNoCallbackSimple → 截图 → updateGoodsNoCallback
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.browser.browser_manager import BrowserManager
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.ticket_creator import TicketCreator
from src.order.add_no_callback import send_add_no_callback
from src.order.added_cart_callback import send_added_cart_callback
from src.order.update_goods_no_callback import send_update_goods_no_callback
from src.payment.confirm_page_verifier import (
    check_cart_goods_simple,
    take_full_page_screenshot,
    upload_screenshot_get_url,
)
from src.utils.logger import LoggerMixin

from playbook.extract import parse_int, scrape_int, scrape_text, scrape_yen
from playbook.locator import click_first, ensure_checked, fill_first, find_first, page_has_any_text
from playbook.verify import has_configured_arrived, wait_arrived
from playbook.walk import (
    collect_specs_of,
    harvest,
    has_action,
    missing_required,
    need_arrived,
    normalize_pages,
    page_label,
)


class PlaybookOrderProcessor(LoggerMixin):
    def __init__(self, config: Dict[str, Any], browser_manager: BrowserManager):
        self.config = config
        self.browser_manager = browser_manager
        self.pb: Dict[str, Any] = config.get("_playbook") if isinstance(config.get("_playbook"), dict) else {}
        self.ticket_creator = TicketCreator(config)
        self.feishu_notifier = FeishuNotifier(config)

    def _stage(self, name: str) -> Dict[str, Any]:
        raw = self.pb.get(name)
        return raw if isinstance(raw, dict) else {}

    def process_order(self, order: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        order_id = order.get("order_id", "未知")
        if self.config.get("_playbook_error"):
            msg = "站点 YAML 未加载: %s" % self.config.get("_playbook_error")
            self._fail(order, [msg], "配置")
            return False, self._summary(order, failure_reason=msg)

        products: List[Dict[str, Any]] = list(order.get("products") or [])
        self.logger.info("playbook：开始订单 %s，商品数 %s", order_id, len(products))
        if not products:
            return False, self._summary(order, failure_reason="订单无商品")

        flow = str(self.pb.get("flow") or "no_cart").strip() or "no_cart"
        use_cart = flow == "cart" or bool(self._stage("cart").get("enabled"))
        if not use_cart:
            products = products[:1]

        try:
            self._maybe_login()
        except Exception as e:
            self._fail(order, ["登录失败: %s" % e], "登录")
            return False, self._summary(order, failure_reason="登录失败: %s" % e)

        if use_cart:
            try:
                self._maybe_clear_cart()
            except Exception as e:
                self._fail(order, ["清空购物车失败: %s" % e], "清车")
                return False, self._summary(order, failure_reason="清空购物车失败: %s" % e)

        driver = self.browser_manager.get_driver()
        ident = self._stage("identity")
        store_name = self._store_name(ident)
        use_curl = bool((self.config.get("order_api") or {}).get("use_curl_for_order_api", True))
        inspect = self._stage("inspect")
        add_cart = self._stage("add_cart")
        check_cfg = self._stage("check_cart")
        confirm_cfg = self._stage("confirm_pay")
        complete_cfg = self._stage("complete")

        scraped: List[Dict[str, Any]] = []
        first_item_id = ""

        # ① 价格库存 → ② 加购 → addedCart
        for product in products:
            product_url = str(product.get("url") or "").strip()
            if not product_url:
                self._fail(order, ["商品 URL 为空"], "价格库存")
                return False, self._summary(order, failure_reason="商品 URL 为空")
            item_id = self._extract_item_id(product_url)
            first_item_id = first_item_id or item_id
            if not item_id:
                msg = "无法解析 item_id: %s" % product_url
                self._fail(order, [msg], "价格库存")
                return False, self._summary(order, failure_reason=msg)

            sold = self._detect_sold_via_api(item_id, product_url)
            if sold:
                self._fail(order, [sold], "价格库存")
                return False, self._summary(order, failure_reason=sold)

            try:
                self.browser_manager.navigate(product_url)
            except Exception as e:
                msg = "打开商品页失败: %s" % e
                self._fail(order, [msg], "价格库存")
                return False, self._summary(order, failure_reason=msg)
            time.sleep(float(inspect.get("wait_after_load_seconds") or 2))

            info, err = self._inspect_price_stock(driver, inspect, product, item_id, product_url)
            if err:
                self._fail(order, [err], "价格库存")
                return False, self._summary(order, failure_reason=err)

            spec_cfg = self._stage("spec_check")
            if spec_cfg.get("enabled"):
                ok_spec, spec_msg = self._check_spec(driver, product, spec_cfg)
                if not ok_spec:
                    self._fail(order, [spec_msg], "规格")
                    return False, self._summary(order, failure_reason=spec_msg)

            qty = int(product.get("quantity") or 1)
            if inspect.get("quantity_input") and qty > 1:
                if not fill_first(driver, inspect.get("quantity_input"), str(qty), timeout=4, logger=self.logger):
                    msg = "无法填写数量 qty=%s item=%s" % (qty, item_id)
                    self._fail(order, [msg], "价格库存")
                    return False, self._summary(order, failure_reason=msg)

            from src.utils.dev_test import stop_before_purchase as _dev_stop_buy

            if _dev_stop_buy(self.config):
                self.logger.warning("本地测试：价格库存已过，停在加购前 item=%s", item_id)
                return True, self._summary(order, success=True, failure_reason="本地测试：停在购买前")

            btn = add_cart.get("button") or inspect.get("buy_button") or inspect.get("add_to_cart_button")
            wait_btn = float(add_cart.get("button_wait_seconds") or 20)
            if find_first(driver, btn, timeout=wait_btn, logger=self.logger) is None:
                msg = "未找到加购/购买按钮 item=%s" % item_id
                self._fail(order, [msg], "加购")
                return False, self._summary(order, failure_reason=msg)
            self._pre_click_wait("加入购物车")
            if not click_first(driver, btn, timeout=5, logger=self.logger):
                msg = "点击加购/购买失败 item=%s" % item_id
                self._fail(order, [msg], "加购")
                return False, self._summary(order, failure_reason=msg)
            time.sleep(float(add_cart.get("wait_after_click_seconds") or 2))
            self._dismiss_modals(driver, add_cart)

            ok_arr, arr_msg = wait_arrived(
                driver,
                add_cart.get("arrived"),
                timeout=float((add_cart.get("arrived") or {}).get("wait_seconds") or 20),
                item_id=item_id,
                logger=self.logger,
                step_name="加购成功",
            )
            if not ok_arr:
                self._fail(order, [arr_msg, "当前 URL: %s" % (driver.current_url or "")], "加购")
                return False, self._summary(order, failure_reason=arr_msg)

            row = {
                "item_id": item_id,
                "goods_no": self._goods_no(ident, product, item_id),
                "name": info.get("name") or product.get("name") or item_id,
                "price": info.get("price"),
                "quantity": qty,
                "url": product_url,
                "product": product,
            }
            scraped.append(row)
            ok_cb, cb_msg = self._added_cart(order, row, store_name, use_curl)
            if not ok_cb:
                msgs = ["addedCartCallbackSimple 未成功（Message=%s）" % (cb_msg or "-")]
                self._fail(order, msgs, "addedCart")
                return False, self._summary(order, failure_reason="addedCart 失败: %s" % (cb_msg or "-"))

        # ③ checkCart：可走多页；金额选择器对每一页都试，后出现的覆盖先出现的
        if len(products) <= 1:
            from_page = str(check_cfg.get("single_from") or "next_page")
        else:
            from_page = str(check_cfg.get("multi_from") or "cart")
        err_nav = self._goto_check_cart_page(driver, from_page, check_cfg, use_cart)
        if err_nav:
            self._fail(order, [err_nav], "checkCart")
            return False, self._summary(order, failure_reason=err_nav)
        chk_skip = list(check_cfg.get("skip_order_texts") or []) + list(
            (self._stage("threeds").get("detect_texts") or [])
        )
        walk_err, chk_bag = self._walk_pages(
            driver,
            check_cfg,
            item_id=first_item_id,
            collect_specs=collect_specs_of(check_cfg),
            skip_texts=chk_skip,
            stage_name="checkCart",
            click_last_next=False,
            order=order,
            shot_keys=("price", "total", "rows"),
            shot_reason="checkCart 截图",
        )
        if walk_err:
            self._fail(order, [walk_err], "checkCart")
            return False, self._summary(order, failure_reason=walk_err)
        last_price = chk_bag.get("total") or chk_bag.get("price") or next(
            (int(g["price"]) for g in scraped if g.get("price")), None
        )
        if last_price:
            chk_bag["price"] = chk_bag.get("price") or last_price
            chk_bag["total"] = chk_bag.get("total") or last_price
        miss = missing_required(chk_bag, list(check_cfg.get("required") or ["price"]))
        if miss:
            msg = "走完 checkCart 所有确认页仍缺字段 %s（选择器会对每一页都试，不必事先指定在哪一页）" % ",".join(miss)
            self._fail(order, [msg], "checkCart")
            return False, self._summary(order, failure_reason=msg)
        if chk_bag.get("price"):
            for g in scraped:
                g["price"] = int(chk_bag.get("price") or g.get("price") or 0)
        rows = chk_bag.get("rows") or []
        if rows and check_cfg.get("verify_quantity", True):
            want = sum(int(p.get("quantity") or 1) for p in products)
            got = sum(parse_int(r.get("quantity") or "") or 1 for r in rows)
            if got != want:
                msg = "购物车/结算行数量与订单不符 订单=%s 页面=%s" % (want, got)
                self._fail(order, [msg], "checkCart")
                return False, self._summary(order, failure_reason=msg)
        shot_url = chk_bag.get("_shot_url") or self._take_shot_upload(driver, order, "checkCart 截图")
        if not shot_url:
            return False, self._summary(order, failure_reason="checkCart 截图上传失败")
        goods_list = [
            {
                "No": g.get("goods_no"),
                "Num": int(g.get("quantity") or 1),
                "StoreName": store_name,
                "Price": int(g.get("price") or last_price),
            }
            for g in scraped
        ]
        total = int(chk_bag.get("total") or last_price)
        ok_chk, chk_err, chk_raw = check_cart_goods_simple(
            order,
            total=total,
            goods_fee=int(chk_bag.get("goods_fee") or last_price),
            operate_fee=int(chk_bag.get("operate_fee") or 0),
            screenshot_url=shot_url,
            config=self.config,
            goods_list_override=goods_list,
            use_curl=use_curl,
        )
        if not ok_chk:
            self._fail(order, ["checkCartGoodsSimple 失败: %s" % chk_err], "checkCart")
            return False, self._summary(
                order,
                failure_reason=chk_err or "checkCart 失败",
                check_cart_requested=True,
                check_cart_response=(chk_raw or "")[:500],
            )
        self.logger.info(
            "playbook：checkCartGoodsSimple 成功 total=%s（数据页=%s）",
            total,
            chk_bag.get("_harvest_page") or "-",
        )

        # ④⑤ 确认支付多页 + 注文号（可能在其中任一页，或点完后的成功页）
        pay_collect = collect_specs_of(
            confirm_cfg,
            complete_cfg.get("extract") if isinstance(complete_cfg.get("extract"), dict) else {},
        )
        after_ok = confirm_cfg.get("success_arrived") or complete_cfg.get("arrived")
        if not confirm_cfg.get("pages"):
            after_ok = after_ok or confirm_cfg.get("arrived")
        walk_err, pay_bag = self._walk_pages(
            driver,
            confirm_cfg,
            item_id=first_item_id,
            collect_specs=pay_collect,
            skip_texts=list(self._stage("threeds").get("detect_texts") or []),
            stage_name="确认支付",
            click_last_next=True,
            after_last_arrived=after_ok,
            treat_top_arrived_as_success=not bool(confirm_cfg.get("pages")),
            order=order,
            shot_keys=("purchase_no",),
            shot_reason="成功页截图",
        )
        if walk_err:
            self._fail(order, [walk_err, "当前 URL: %s" % (driver.current_url or "")], "确认支付")
            return False, self._summary(
                order,
                failure_reason=walk_err,
                check_cart_requested=True,
                check_cart_response="ok",
            )
        purchase_no = str(pay_bag.get("purchase_no") or "").strip()
        if not purchase_no:
            src = str(ident.get("purchase_no_source") or complete_cfg.get("purchase_no_source") or "page")
            if src == "item_id" or complete_cfg.get("allow_item_id_fallback"):
                purchase_no = first_item_id
                self.logger.warning("playbook：未抓到注文番号，按配置回退 item_id=%s", purchase_no)
            elif src == "url":
                purchase_no = self._extract_item_id(driver.current_url or "") or first_item_id
        if not purchase_no:
            msg = "走完确认页仍未抓到注文番号，不能当作下单成功"
            self._fail(order, [msg, "当前 URL: %s" % (driver.current_url or "")], "下单成功")
            return False, self._summary(
                order,
                failure_reason=msg,
                check_cart_requested=True,
                check_cart_response="ok",
            )
        self.logger.info(
            "playbook：注文番号=%s（数据页=%s），确认下单成功",
            purchase_no,
            pay_bag.get("_harvest_page") or "-",
        )

        purchase_url = str(pay_bag.get("purchase_url") or driver.current_url or "").strip()
        url_tpl = str(
            ident.get("purchase_url_template")
            or (self.config.get("order_api") or {}).get("purchase_url_template")
            or "{url}"
        )
        purchase_url = (
            url_tpl.replace("{purchase_no}", purchase_no)
            .replace("{item_id}", first_item_id)
            .replace("{url}", purchase_url)
        )
        credit = str(
            (self.config.get("payment") or {}).get("add_no_credit_card") or ident.get("credit_card") or ""
        ).strip()
        ok_add, add_err, add_raw = send_add_no_callback(
            order,
            [{"no": purchase_no, "url": purchase_url}],
            credit_card=credit,
            config=self.config,
            use_curl=use_curl,
        )
        if not ok_add:
            self._fail(order, ["addNoCallbackSimple 失败: %s" % add_err], "addNo")
            return False, self._summary(
                order,
                failure_reason=add_err or "addNo 失败",
                check_cart_requested=True,
                check_cart_response="ok",
                add_no_requested=True,
                add_no_response=(add_raw or "")[:500],
            )

        success_shot = pay_bag.get("_shot_url") or self._take_shot_upload(driver, order, "成功页截图") or ""
        goods_no_list = [
            {"no": g.get("goods_no"), "price": int(g.get("price") or last_price or 0), "num": int(g.get("quantity") or 1)}
            for g in scraped
        ]
        ok_u, uerr = send_update_goods_no_callback(
            order,
            purchase_no,
            goods_no_list,
            success_shot,
            store_name,
            config=self.config,
            use_curl=use_curl,
        )
        if not ok_u:
            self.logger.warning("updateGoodsNoCallback 失败: %s", uerr)

        self.logger.info("playbook：订单 %s 完成 purchase_no=%s", order_id, purchase_no)
        return True, self._summary(
            order,
            success=True,
            check_cart_requested=True,
            check_cart_response="ok",
            add_no_requested=True,
            add_no_response="ok",
        )

    def _inspect_price_stock(
        self, driver, inspect: Dict[str, Any], product: Dict[str, Any], item_id: str, product_url: str
    ) -> Tuple[Dict[str, Any], str]:
        scrape = inspect.get("scrape") if isinstance(inspect.get("scrape"), dict) else {}
        if page_has_any_text(driver, list(inspect.get("sold_texts") or [])):
            return {}, "商品不可购/已售出 item=%s url=%s" % (item_id, product_url)
        name = scrape_text(driver, scrape.get("name"), timeout=3, logger=self.logger) if scrape.get("name") else ""
        price = scrape_yen(driver, scrape.get("price"), timeout=3, logger=self.logger)
        if not price:
            price = self._yen_from_product(product)
        if inspect.get("require_price", True) and not price:
            return {}, "商品页未抓到价格 item=%s（请配 inspect.scrape.price）" % item_id
        stock = None
        if scrape.get("stock"):
            stock = scrape_int(driver, scrape.get("stock"), timeout=3, logger=self.logger)
        want = int(product.get("quantity") or 1)
        if stock is not None and stock <= 0:
            return {}, "库存为 0 item=%s" % item_id
        if stock is not None and stock < want:
            return {}, "库存不足 item=%s 需要=%s 页面=%s" % (item_id, want, stock)
        ok_arr, arr_msg = True, ""
        if inspect.get("arrived"):
            ok_arr, arr_msg = wait_arrived(
                driver, inspect.get("arrived"), timeout=8, item_id=item_id, logger=self.logger, step_name="商品页"
            )
            if not ok_arr:
                return {}, arr_msg
        self.logger.info("playbook：价格库存通过 item=%s price=%s stock=%s name=%s", item_id, price, stock, name or "-")
        return {"name": name, "price": price, "stock": stock}, ""

    def _goto_check_cart_page(self, driver, from_page: str, check_cfg: Dict[str, Any], use_cart: bool) -> str:
        from_page = (from_page or "next_page").strip()
        if from_page in ("next_page", "next", "after_add"):
            self.logger.info("playbook：单品 checkCart 使用加购后当前页")
            return ""
        if from_page == "cart":
            url = str(check_cfg.get("cart_url") or self._stage("cart").get("url") or "").strip()
            if url:
                self.browser_manager.navigate(url)
            return ""
        if from_page == "checkout":
            cart_url = str(check_cfg.get("cart_url") or self._stage("cart").get("url") or "").strip()
            checkout_url = str(check_cfg.get("checkout_url") or "").strip()
            if checkout_url:
                self.browser_manager.navigate(checkout_url)
                return ""
            if cart_url:
                self.browser_manager.navigate(cart_url)
                time.sleep(float(check_cfg.get("wait_after_load_seconds") or 1.5))
            go = check_cfg.get("go_checkout_button") or self._stage("cart").get("checkout_button")
            if go:
                self._pre_click_wait("去结算")
                if not click_first(driver, go, timeout=8, logger=self.logger):
                    return "多品：未能从购物车点到结算页"
            return ""
        return "未知的 check_cart 来源: %s（single_from/multi_from 用 next_page / cart / checkout）" % from_page

    def _walk_pages(
        self,
        driver,
        stage: Dict[str, Any],
        *,
        item_id: str,
        collect_specs: Dict[str, Any],
        skip_texts: List[str],
        stage_name: str,
        click_last_next: bool = True,
        after_last_arrived: Any = None,
        treat_top_arrived_as_success: bool = False,
        order: Optional[Dict[str, Any]] = None,
        shot_keys: Tuple[str, ...] = (),
        shot_reason: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """走完一个接口下的全部确认页。字段在每一页都试抓，后非空覆盖先前。"""
        pages = normalize_pages(stage, treat_top_arrived_as_success=treat_top_arrived_as_success)
        bag: Dict[str, Any] = {}
        if not pages:
            return "「%s」未配置 pages，也没有可兼容的单页 button/arrived" % stage_name, bag

        def _merge_specs(page: Dict[str, Any]) -> Dict[str, Any]:
            specs = dict(collect_specs or {})
            for blob in (page.get("collect"), page.get("extract")):
                if isinstance(blob, dict):
                    specs.update(blob)
            return specs

        def _shot_if_needed(updated: List[str]) -> str:
            if not order or not shot_keys:
                return ""
            if not any(k in updated for k in shot_keys):
                return ""
            url = self._take_shot_upload(driver, order, shot_reason or (stage_name + " 截图"), notify=False)
            if not url:
                return "%s：截图上传失败（数据页=%s）" % (shot_reason or stage_name, bag.get("_harvest_page") or "-")
            bag["_shot_url"] = url
            return ""

        for i, page in enumerate(pages):
            name = page_label(page, i)
            is_last = i == len(pages) - 1
            wait_load = page.get("wait_after_load_seconds")
            if wait_load in (None, ""):
                wait_load = stage.get("wait_after_load_seconds")
            time.sleep(float(wait_load or 1.2))
            self._dismiss_modals(driver, page if page.get("modal_ok") else stage)
            if need_arrived(page):
                ok_arr, arr_msg = wait_arrived(
                    driver,
                    page.get("arrived"),
                    timeout=float((page.get("arrived") or {}).get("wait_seconds") or 20),
                    item_id=item_id,
                    logger=self.logger,
                    step_name="%s/%s" % (stage_name, name),
                )
                if not ok_arr:
                    return arr_msg, bag
            page_skip = list(page.get("skip_order_texts") or []) + list(skip_texts or [])
            if page_has_any_text(driver, page_skip):
                return "「%s/%s」出现需人工处理文案 URL=%s" % (stage_name, name, driver.current_url or ""), bag
            for box in list(page.get("checkboxes") or []):
                loc = box.get("locators") if isinstance(box, dict) and "locators" in box else box
                required = True if not isinstance(box, dict) else box.get("must_check", True)
                ok_box = ensure_checked(driver, loc, timeout=4, logger=self.logger)
                if required and not ok_box:
                    return "「%s/%s」未能勾选确认项" % (stage_name, name), bag
            updated = harvest(driver, _merge_specs(page), bag, logger=self.logger, page_name="%s/%s" % (stage_name, name))
            if updated:
                bag["_harvest_page"] = name
            shot_err = _shot_if_needed(updated)
            if shot_err:
                return shot_err, bag
            nxt = page.get("next_button") or page.get("button")
            should_click = has_action(nxt) and (not is_last or click_last_next)
            if not is_last and not has_action(nxt):
                return "「%s/%s」没有下一步按钮，无法进入后续页" % (stage_name, name), bag
            if is_last and click_last_next and not has_action(nxt):
                return "「%s」最后一页没有确定/下一步按钮" % stage_name, bag
            if not should_click:
                continue
            self._pre_click_wait("%s/%s" % (stage_name, name))
            if not click_first(driver, nxt, timeout=8, logger=self.logger):
                return "「%s/%s」点击下一步失败" % (stage_name, name), bag
            wait_click = page.get("wait_after_click_seconds")
            if wait_click in (None, ""):
                wait_click = stage.get("wait_after_click_seconds")
            time.sleep(float(wait_click or 2))
            self._dismiss_modals(driver, page if page.get("modal_ok") else stage)

        if click_last_next:
            if not has_configured_arrived(after_last_arrived):
                return "未配置「%s成功页」到达校验（请填 confirm_pay.success_arrived 的 URL / 标题 / 文案）" % stage_name, bag
            ok_arr, arr_msg = wait_arrived(
                driver,
                after_last_arrived,
                timeout=float((after_last_arrived or {}).get("wait_seconds") or 90),
                item_id=item_id,
                logger=self.logger,
                step_name=stage_name + "成功页",
            )
            if not ok_arr:
                return arr_msg, bag
            updated = harvest(
                driver,
                collect_specs or {},
                bag,
                logger=self.logger,
                page_name=stage_name + "/成功页",
            )
            if updated:
                bag["_harvest_page"] = "成功页"
            shot_err = _shot_if_needed(updated)
            if shot_err:
                return shot_err, bag
        return "", bag

    def _added_cart(self, order: Dict[str, Any], row: Dict[str, Any], store_name: str, use_curl: bool) -> Tuple[bool, str]:
        product = dict(row.get("product") or {})
        product["goods_no"] = row.get("goods_no")
        product["shop_id"] = store_name
        product["quantity"] = int(row.get("quantity") or 1)
        product["price"] = row.get("price")
        product["name"] = row.get("name")
        try:
            return send_added_cart_callback(
                order, product, config=self.config, is_lack=0, is_limit=0, use_curl=use_curl
            )
        except Exception as e:
            return False, str(e)

    def _fail(self, order: Dict[str, Any], messages: List[str], reason: str) -> None:
        self._handle_order_issue(order, messages, reason=reason)

    def _take_shot_upload(self, driver, order: Dict[str, Any], reason: str, *, notify: bool = True) -> Optional[str]:
        path = None
        try:
            path = take_full_page_screenshot(driver)
            url = upload_screenshot_get_url(path, self.config)
            if not url:
                if notify:
                    self._fail(order, ["%s：截图上传失败" % reason], "截图")
                return None
            return url
        except Exception as e:
            if notify:
                self._fail(order, ["%s：截图异常 %s" % (reason, e)], "截图")
            return None
        finally:
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass

    def _store_name(self, ident: Dict[str, Any]) -> str:
        return str(
            self.config.get("_playbook_store_name")
            or ident.get("store_name")
            or self.pb.get("store_name")
            or (self.config.get("_site") or {}).get("id")
            or "playbook"
        ).strip()

    def _goods_no(self, ident: Dict[str, Any], product: Dict[str, Any], item_id: str) -> str:
        mode = str(ident.get("goods_no_source") or "item_id")
        if mode == "item_id":
            return item_id
        return str(product.get("goods_no") or product.get("no") or item_id).strip()

    def _maybe_login(self) -> None:
        login_pb = self._stage("login")
        if login_pb.get("enabled") is False:
            return
        cfg_login = self.config.get("login") if isinstance(self.config.get("login"), dict) else {}
        email = str(cfg_login.get("email") or "").strip()
        password = str(cfg_login.get("password") or "").strip()
        driver = self.browser_manager.get_driver()
        url = str(login_pb.get("url") or (self.config.get("_site") or {}).get("manual_login_url") or "").strip()
        hints = [str(x) for x in (login_pb.get("login_url_contains") or []) if x]
        logged_hints = [str(x) for x in (login_pb.get("logged_in_url_contains") or []) if x]
        logged_sel = login_pb.get("logged_in_indicator")
        cur = (driver.current_url or "") if driver else ""
        if logged_sel and find_first(driver, logged_sel, timeout=1.5, logger=self.logger) is not None:
            self.logger.info("playbook：已登录，跳过自动登录")
            return
        if logged_hints and any(h in cur for h in logged_hints):
            return
        if url:
            self.browser_manager.navigate(url)
            time.sleep(float(login_pb.get("wait_after_open_seconds") or 2))
            cur = driver.current_url or ""
            if hints and not any(h in cur for h in hints):
                if find_first(driver, login_pb.get("username"), timeout=2, logger=self.logger) is None:
                    self.logger.info("playbook：未出现登录表单，假定会话仍有效")
                    return
        if not email or not password:
            self.logger.warning("playbook：config.yaml 未填 login.email/password，请手动登录")
            return
        if login_pb.get("username") and not fill_first(
            driver, login_pb.get("username"), email, timeout=8, logger=self.logger
        ):
            raise RuntimeError("找不到用户名输入框")
        if login_pb.get("submit_after_username") and login_pb.get("submit"):
            click_first(driver, login_pb.get("submit"), timeout=5, logger=self.logger)
            time.sleep(1.2)
        if login_pb.get("password") and not fill_first(
            driver, login_pb.get("password"), password, timeout=8, logger=self.logger
        ):
            raise RuntimeError("找不到密码输入框")
        if login_pb.get("submit"):
            click_first(driver, login_pb.get("submit"), timeout=5, logger=self.logger)
        time.sleep(float(login_pb.get("wait_after_submit_seconds") or 3))

    def _maybe_clear_cart(self) -> None:
        cart = self._stage("cart")
        if not cart.get("enabled"):
            return
        url = str(cart.get("url") or "").strip()
        if url:
            self.browser_manager.navigate(url)
            time.sleep(float(cart.get("wait_after_load_seconds") or 2))
        driver = self.browser_manager.get_driver()
        empty_texts = [str(x) for x in (cart.get("empty_texts") or []) if x]
        if page_has_any_text(driver, empty_texts):
            return
        if cart.get("clear_all") and click_first(driver, cart.get("clear_all"), timeout=6, logger=self.logger):
            time.sleep(float(cart.get("wait_after_clear_seconds") or 1.5))
            return
        remove = cart.get("clear_item")
        if not remove:
            raise RuntimeError("已开启清车但未配置 clear_all / clear_item")
        for _ in range(int(cart.get("max_item_removes") or 30)):
            if page_has_any_text(driver, empty_texts):
                return
            if not click_first(driver, remove, timeout=2, logger=self.logger):
                break
            time.sleep(float(cart.get("wait_after_clear_seconds") or 1.0))

    def _check_spec(self, driver, product: Dict[str, Any], spec_cfg: Dict[str, Any]) -> Tuple[bool, str]:
        want = str(product.get("variant_id") or product.get("spec") or product.get("sku") or "").strip()
        if not want:
            return True, ""
        el = find_first(driver, spec_cfg.get("option"), timeout=float(spec_cfg.get("wait_seconds") or 5), logger=self.logger)
        if el is None:
            return False, "规格校验开启但未找到规格控件"
        shown = " ".join((el.text or "").split())
        if want and shown and want not in shown:
            return False, "规格不一致 期望=%s 页面=%s" % (want, shown)
        return True, ""

    def _detect_sold_via_api(self, item_id: str, product_url: str) -> str:
        api = self._stage("item_api")
        if not api.get("enabled"):
            return ""
        tpl = str(api.get("url_template") or "").strip()
        if not tpl:
            return ""
        url = tpl.replace("{item_id}", item_id)
        sold_status = [str(x).upper() for x in (api.get("sold_status_values") or ["SOLD"])]
        try:
            import requests

            r = requests.get(url, timeout=float(api.get("timeout_seconds") or 12))
            data = r.json() if r.ok else {}
        except Exception as e:
            self.logger.warning("playbook：商品 API 失败，改走页面判断: %s", e)
            return ""
        st = str((data or {}).get(str(api.get("status_field") or "status")) or "").upper()
        if st in sold_status:
            return "商品已售出（API status=%s） item=%s url=%s" % (st, item_id, product_url)
        return ""

    def _extract_item_id(self, url: str) -> str:
        ident = self._stage("identity")
        pattern = str(ident.get("item_id_regex") or "").strip()
        if pattern:
            try:
                m = re.search(pattern, url or "")
                if m:
                    if m.lastindex:
                        return str(m.group(1) or m.group(0))
                    return str(m.group(0))
            except Exception:
                pass
        path = urlparse(url or "").path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        if "item" in parts:
            i = parts.index("item")
            if i + 1 < len(parts):
                return parts[i + 1]
        qs = urlparse(url or "").query
        for kv in qs.split("&"):
            if kv.startswith("item_id="):
                return kv.split("=", 1)[-1]
        return parts[-1] if parts else ""

    def _yen_from_product(self, product: Dict[str, Any]) -> Optional[int]:
        for k in ("price", "unit_price", "amount"):
            n = parse_int(str(product.get(k) if product.get(k) is not None else ""))
            if n and n > 0:
                return n
        return None

    def _dismiss_modals(self, driver, cfg: Dict[str, Any]) -> None:
        modal = cfg.get("modal_ok") if isinstance(cfg, dict) else None
        if not modal:
            return
        for _ in range(3):
            if click_first(driver, modal, timeout=2.0, logger=self.logger):
                time.sleep(0.5)
            else:
                break

    def _pre_click_wait(self, action: str) -> None:
        pay = self.config.get("payment") or {}
        rng = pay.get("pre_click_wait_seconds_range") or [0.7, 1.8]
        try:
            mn, mx = float(rng[0]), float(rng[1])
        except Exception:
            mn, mx = 0.7, 1.8
        if mx <= 0:
            return
        sec = random.uniform(max(0.0, mn), max(0.0, mx))
        self.logger.info("关键点击前随机等待 %.2f 秒（%s）", sec, action)
        time.sleep(sec)

    def _handle_order_issue(self, order: Dict[str, Any], messages: List[str], reason: str = "") -> None:
        order_id = order.get("order_id", "未知")
        user_id = order.get("user_id")
        if not messages:
            return
        if reason:
            self.logger.warning("订单 %s %s:", order_id, reason)
        for msg in messages:
            self.logger.warning("  - %s", msg)
        try:
            self.ticket_creator.create_ticket(order_id, messages, user_id=user_id)
        except Exception as e:
            self.logger.error("创建工单失败: %s", e)
        try:
            extra = (reason + "，已创建工单。") if reason else "已创建工单。"
            self.feishu_notifier.notify_order_issue(order_id, messages, user_id=user_id, extra=extra)
        except Exception as e:
            self.logger.warning("飞书提醒失败: %s", e)

    def _summary(
        self,
        order: Dict[str, Any],
        *,
        success: bool = False,
        failure_reason: str = "",
        check_cart_requested: bool = False,
        check_cart_response: str = "未请求",
        add_no_requested: bool = False,
        add_no_response: str = "未请求",
        update_errors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return {
            "order_no": str(order.get("order_no") or order.get("order_id") or ""),
            "success": success,
            "payment_method": "playbook",
            "failure_reason": failure_reason,
            "check_cart_requested": check_cart_requested,
            "check_cart_response": check_cart_response,
            "add_no_requested": add_no_requested,
            "add_no_response": add_no_response,
            "update_errors": update_errors or [],
        }
