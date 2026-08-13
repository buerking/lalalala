"""
订单获取器
支持：1）真实订单接口（待处理订单ID列表 + POST getOrderSimple 验签）；2）本地 JSON 文件（test.json / 正式格式）
"""

import json
import os
import ssl
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from requests.adapters import HTTPAdapter

from src.utils.logger import LoggerMixin
from src.utils.retry import retry
from src.utils.sign_generator import SignGenerator
from src.utils.paypay_queue import enqueue_paypay_order
from src.notification.feishu_notifier import FeishuNotifier


def _print_debug(label: str, *args) -> None:
    """保证控制台能看到：验签参数、Sign、payload 等，便于与手动测试对比"""
    msg = " ".join(str(x) for x in args)
    print(f"[订单接口调试] {label} {msg}", flush=True)


class TLS12Adapter(HTTPAdapter):
    """强制使用 TLS 1.2，避免部分环境出现 SSL: UNEXPECTED_EOF_WHILE_READING"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


class OrderFetcher(LoggerMixin):
    """订单获取器"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化订单获取器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.api_config = config.get('order_api', {})
        self.base_url = self.api_config.get('base_url', '')
        self.endpoint = self.api_config.get('endpoint', '')
        self.headers = self.api_config.get('headers', {})
        self.params = self.api_config.get('params', {})
        self.timeout = self.api_config.get('timeout', 30)
        # 真实订单接口：获取订单详情 POST 地址、验签密钥、操作人标记、待处理订单 ID 列表文件
        self.get_order_detail_url = (self.api_config.get('get_order_detail_url') or '').strip()
        # 拉单列表接口（可显式配置；否则由 get_order_detail_url 中的 getOrderSimple 替换为 getOrderListSimple）
        self.get_order_list_simple_url = (self.api_config.get('get_order_list_simple_url') or '').strip()
        self.get_order_list_group_ids = self.api_config.get('get_order_list_group_ids', [265])
        self.get_order_list_page_size = int(self.api_config.get('get_order_list_page_size', 20) or 20)
        # 与 Apipost 一致：空 OrderId 是否参与验签（多数后端「空串」不参与签名，仅表单带 OrderId=）
        self.get_order_list_sign_omit_empty_order_id = bool(
            self.api_config.get('get_order_list_sign_omit_empty_order_id', True)
        )
        self.secret = self.api_config.get('secret', '')
        self.pc_mark = self.api_config.get('pc_mark', 'surugaya')
        self.pending_order_ids_file = self.api_config.get('pending_order_ids_file', 'pending_orders_demo.json')
        # SSL：部分环境 Python 请求会报 UNEXPECTED_EOF_WHILE_READING，可强制 TLS1.2 或关闭校验证书
        self.verify_ssl = self.api_config.get('verify_ssl', True)
        self.use_tls12 = self.api_config.get('use_tls12', True)
        # 使用系统 curl 发 POST（绕过 Python requests 的 SSL 问题，与 Apipost/手动测试一致）
        self.use_curl_for_order_api = self.api_config.get('use_curl_for_order_api', False)
    
    @retry(max_attempts=3, delay=5.0)
    def fetch_orders(self) -> List[Dict[str, Any]]:
        """
        获取符合条件的订单列表
        
        Returns:
            订单列表，每个订单包含订单ID、状态、商品列表等信息
            
        Raises:
            requests.RequestException: API请求失败
            ValueError: 数据解析失败
        """
        # 优先：真实订单接口（待处理订单 ID 列表 + POST getOrderSimple 验签）
        if self.get_order_detail_url and self.secret:
            return self._fetch_from_real_order_api()
        
        if not self.base_url:
            raise ValueError("订单API基础URL未配置（或未配置 get_order_detail_url + secret）")
        
        base_url_lower = self.base_url.lower().strip()
        is_url = base_url_lower.startswith('http://') or base_url_lower.startswith('https://')
        
        if is_url:
            return self._fetch_from_api()
        else:
            return self._fetch_from_local_file()
    
    def _fetch_from_local_file(self) -> List[Dict[str, Any]]:
        """
        从本地JSON文件读取订单数据
        
        Returns:
            订单列表
        """
        file_path = self.base_url.strip()
        
        # 如果是相对路径，从项目根目录查找
        if not os.path.isabs(file_path):
            project_root = Path(__file__).parent.parent.parent
            
            # 处理相对路径（如 ../test.json 或 ./test.json）
            if file_path.startswith('../'):
                # 从项目根目录的父目录查找
                file_path = project_root.parent / file_path[3:]
            elif file_path.startswith('./'):
                # 从项目根目录查找
                file_path = project_root / file_path[2:]
            elif file_path.startswith('/'):
                # 以 / 开头，从项目根目录查找（去掉开头的 /）
                file_path = project_root / file_path.lstrip('/')
            else:
                # 普通相对路径，从项目根目录查找
                file_path = project_root / file_path
        
        file_path = Path(file_path).resolve()  # 解析为绝对路径
        file_path = str(file_path)
        
        self.logger.info(f"从本地文件读取订单数据: {file_path}")
        
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"订单数据文件不存在: {file_path}")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 兼容正式接口格式：{ "Success": true, "Data": { "OrderId", "List": [...] } }
            if isinstance(data, dict) and 'Success' in data and 'Data' in data:
                orders = self._parse_formal_api_response(data)
            else:
                # 解析旧版 test.json 格式: {"ORD001": [...], "ORD002": [...]}
                orders = []
                for order_id, products in data.items():
                    order = self._parse_test_order(order_id, products)
                    orders.append(order)
            
            self.logger.info(f"成功从文件读取 {len(orders)} 个订单")
            return orders
        
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON文件格式错误: {e}")
            raise ValueError(f"JSON文件格式错误: {e}")
        except Exception as e:
            self.logger.error(f"读取订单文件失败: {e}")
            raise
    
    def _post_with_curl(self, url: str, body: Dict[str, Any], timeout: int) -> Tuple[int, str]:
        """用系统 curl 发 POST（application/x-www-form-urlencoded），与 requests 行为一致，避免手写拼接出错。"""
        pairs: List[Tuple[str, str]] = []
        for k, v in body.items():
            if v is None:
                pairs.append((k, ""))
            elif isinstance(v, (list, tuple)):
                for item in v:
                    pairs.append((k, str(item)))
            elif isinstance(v, bool):
                pairs.append((k, "true" if v else "false"))
            else:
                pairs.append((k, str(v)))
        form_str = urlencode(pairs, doseq=True, encoding="utf-8")
        # -w "\n%{http_code}" 让状态码单独占最后一行，便于拆分
        cmd = [
            "curl", "-s", "-w", "\n%{http_code}",
            "-X", "POST",
            "--data", form_str,
            "--connect-timeout", "10",
            "--max-time", str(timeout),
            url,
        ]
        _print_debug("(curl) 执行:", " ".join(cmd[:6]), "...", url[:50])
        try:
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
                code = int(lines[-1])
                body_text = "\n".join(lines[:-1]).strip()
            else:
                code = 0
                body_text = out or (result.stderr or "")
            return code, body_text
        except FileNotFoundError:
            raise RuntimeError("未找到 curl 命令，请安装 curl 或关闭 order_api.use_curl_for_order_api")
        except subprocess.TimeoutExpired:
            raise RuntimeError("curl 请求超时")

    def _fetch_from_real_order_api(self) -> List[Dict[str, Any]]:
        """
        真实订单接口流程：
        1）一次性调用 POST getOrderListSimple，form-data：
           - OrderId：空（用于接口测试）
           - GroupIds：[265]（固定值）
           - PcMark：共用
           - PageSize：20（固定值）
           - Sign：根据上述 form-data 重新生成验签
        2）解析返回的 Success/Data/List 格式，汇总为订单列表。
        """
        # 最终请求 URL（与下方 curl 使用的一致；避免日志仍打印 getOrderSimple 误导排查）
        if self.get_order_list_simple_url:
            url = self.get_order_list_simple_url.strip()
        else:
            url = (self.get_order_detail_url or "").strip()
            if "getOrderSimple" in url:
                url = url.replace("getOrderSimple", "getOrderListSimple")
            else:
                raise ValueError(
                    "请配置 order_api.get_order_list_simple_url，或让 get_order_detail_url 包含 getOrderSimple 以便自动替换为 getOrderListSimple"
                )

        # ---------- 控制台必现：便于与手动验签、Apipost 对比 ----------
        _print_debug("请求 URL(实际 POST):", url)
        _print_debug("PcMark (固定):", self.pc_mark)
        _print_debug("secret 长度 (固定全局密钥，用于 getOrderListSimple 验签):", len(self.secret))
        _print_debug("use_curl_for_order_api:", self.use_curl_for_order_api)

        self.logger.info("[订单接口] 请求 URL: %s, PcMark: %s, verify_ssl: %s, use_tls12: %s",
                         url, self.pc_mark, self.verify_ssl, self.use_tls12)

        sign_generator = SignGenerator(self.secret)

        # 与表单一致：GroupIds 用 JSON 数组字符串（如 [265]），PageSize 用字符串，便于与 PHP $_POST 一致后验签
        gids = self.get_order_list_group_ids
        if not isinstance(gids, list):
            gids = [gids]
        group_ids_str = json.dumps([int(x) for x in gids], separators=(",", ":"), ensure_ascii=False)
        page_size_str = str(self.get_order_list_page_size)

        post_body: Dict[str, str] = {
            "OrderId": "",
            "GroupIds": group_ids_str,
            "PcMark": str(self.pc_mark),
            "PageSize": page_size_str,
        }

        # 验签参数：默认去掉空 OrderId（仍会在 POST 中带 OrderId=）
        sign_params: Dict[str, Any] = dict(post_body)
        if self.get_order_list_sign_omit_empty_order_id and sign_params.get("OrderId", None) == "":
            sign_params.pop("OrderId", None)

        _print_debug("参与签名的参数 sign_params:", sign_params)
        sign = sign_generator.generate_sign(sign_params)
        _print_debug("生成的 Sign:", sign)

        body = dict(post_body)
        body["Sign"] = sign
        _print_debug(
            "POST body (payload):",
            "OrderId=%s, GroupIds=%s, PcMark=%s, PageSize=%s, Sign=%s"
            % (body.get("OrderId"), body.get("GroupIds"), body.get("PcMark"), body.get("PageSize"), body.get("Sign")),
        )

        if self.use_curl_for_order_api:
            status_code, response_text = self._post_with_curl(url, body, self.timeout)
            _print_debug("curl 响应状态码:", status_code)
            _print_debug("curl 响应 body 前 300 字符:", (response_text or "")[:300])
            if status_code != 200:
                raise requests.HTTPError(
                    f"HTTP {status_code}",
                    response=type('R', (), {'status_code': status_code, 'text': response_text})(),
                )
            data = json.loads(response_text) if response_text.strip() else {}
        else:
            post_headers = {k: v for k, v in self.headers.items() if k.lower() != 'content-type'}
            session = requests.Session()
            if self.use_tls12:
                session.mount("https://", TLS12Adapter())
            if not self.verify_ssl:
                self.logger.warning("order_api.verify_ssl 已关闭，仅建议在排查 SSL 问题时临时使用")
            response = session.post(
                url,
                data=body,
                headers=post_headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            self.logger.info("[响应] 状态码: %s, 耗时: %s", response.status_code, getattr(response, "elapsed", None))
            response.raise_for_status()
            data = response.json()
            self.logger.info("[响应] 响应 body 前 200 字符: %s", str(data)[:200])

        # 解析所有订单
        one = self._parse_formal_api_response(data)

        # tenpo_cd（第三方店铺）订单：写入 PayPay 队列文件并跳过本轮自动处理
        orders: List[Dict[str, Any]] = []
        feishu = FeishuNotifier(self.config)
        for o in one:
            products = o.get("products") or []
            has_tenpo_cd = any(p.get("is_third_party") or p.get("shop_type") == 1 for p in products)
            if has_tenpo_cd:
                try:
                    enqueue_paypay_order(self.config, o)
                    self.logger.info(
                        "订单 %s(%s) 商品含 tenpo_cd，已写入 PayPay 队列，跳过本轮自动处理",
                        o.get("order_id"), o.get("order_no") or "",
                    )
                except Exception as q_err:
                    self.logger.warning("写入 PayPay 队列失败，仍跳过本轮自动处理: %s", q_err)
                detail_lines: List[str] = []
                for p in products:
                    if not (p.get("is_third_party") or p.get("shop_type") == 1):
                        continue
                    nm = str(p.get("name") or "未知商品").strip()
                    surl = str(p.get("url") or "").strip()
                    shop_hint = str(p.get("shop_id") or p.get("tenpo_cd") or "").strip()
                    part = f"{nm}"
                    if shop_hint:
                        part += f" | 店铺标识: {shop_hint}"
                    if surl:
                        part += f" | {surl}"
                    detail_lines.append(part)
                if not detail_lines:
                    detail_lines.append("订单含第三方店铺商品（tenpo_cd），详情见后台订单数据")
                try:
                    feishu.notify_paypay_queue_enqueued(
                        str(o.get("order_id") or ""),
                        "tenpo_branch",
                        detail_lines,
                        user_id=o.get("user_id"),
                        order_no=str(o.get("order_no") or "") or None,
                    )
                except Exception as fe_err:
                    self.logger.warning("分店订单入队飞书通知失败: %s", fe_err)
            else:
                orders.append(o)

        self.logger.info(f"成功从真实接口获取 {len(orders)} 个订单（已过滤 tenpo_cd）")
        return orders

    def _load_pending_order_ids(self) -> List[str]:
        """从配置的 JSON 文件加载待处理订单 ID 列表（demo 模拟）。"""
        file_path = self.pending_order_ids_file.strip()
        if not file_path:
            return []

        project_root = Path(__file__).parent.parent.parent
        if not os.path.isabs(file_path):
            if file_path.startswith('../'):
                file_path = project_root.parent / file_path[3:]
            elif file_path.startswith('./') or file_path.startswith('/'):
                file_path = project_root / file_path.lstrip('/')
            else:
                file_path = project_root / file_path
        file_path = Path(file_path).resolve()

        if not file_path.exists():
            self.logger.warning(f"待处理订单 ID 文件不存在: {file_path}")
            return []

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            self.logger.error(f"待处理订单 ID 文件 JSON 格式错误: {e}")
            return []

        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, dict) and "order_ids" in raw:
            return [str(x).strip() for x in raw["order_ids"] if str(x).strip()]
        self.logger.warning("待处理订单 ID 文件格式应为 JSON 数组或 { order_ids: [] }")
        return []

    def _fetch_from_api(self) -> List[Dict[str, Any]]:
        """
        从 API 获取订单数据（GET，旧版；正式环境请用 get_order_detail_url + secret）
        """
        url = f"{self.base_url.rstrip('/')}/{self.endpoint.lstrip('/')}" if self.endpoint else self.base_url.rstrip('/')
        self.logger.info(f"正在从API获取订单列表: {url}")
        try:
            response = requests.get(
                url,
                headers=self.headers,
                params=self.params,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            orders = self._parse_formal_api_response(data)
            self.logger.info(f"成功从API获取 {len(orders)} 个订单")
            return orders
        except requests.RequestException as e:
            self.logger.error(f"获取订单列表失败: {e}")
            raise
        except Exception as e:
            self.logger.error(f"解析订单数据失败: {e}")
            raise ValueError(f"解析订单数据失败: {e}")
    
    def _parse_test_order(self, order_id: str, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        解析test.json格式的订单数据
        
        Args:
            order_id: 订单ID
            products: 商品列表，格式: [{"product_url": "...", "unit_price": 100, "quantity": 1}]
            
        Returns:
            解析后的订单字典
        """
        order = {
            'order_id': order_id,
            'user_id': None,  # 旧版 test.json 无此字段，工单将使用 config 的 default_user_id
            'status': '已支付',  # 测试数据默认状态
            'platform': '骏河屋',
            'products': []
        }
        
        for product_data in products:
            product_url = product_data.get('product_url', '')
            unit_price = float(product_data.get('unit_price', 0))
            quantity = int(product_data.get('quantity', 0))
            
            # 从URL中提取商品名称（作为临时方案）
            # 例如: https://www.suruga-ya.jp/product/detail/BO2416546 -> BO2416546
            product_name = '未知商品'
            if product_url:
                try:
                    # 尝试从URL提取商品ID作为名称
                    parts = product_url.rstrip('/').split('/')
                    if parts:
                        product_name = parts[-1]
                except:
                    pass
            
            product = {
                'name': product_name,
                'url': product_url,
                'price': unit_price,
                'quantity': quantity
            }
            order['products'].append(product)
        
        return order
    
    def _parse_formal_api_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        解析正式接口返回格式：
        {
            "Success": true,
            "Data": {
                "OrderId": "845220",
                "List": [
                    { "GoodsId", "GoodsNo", "GoodsUrl", "GoodsNumber", "GoodsPrice" },
                    ...
                ],
                "Mark": "...",
                "Secret": "..."
            },
            "Message": null,
            "ErrorCode": 400,
            "LangParams": []
        }
        
        Args:
            data: 接口完整响应体
            
        Returns:
            订单列表（通常一条响应对应一个订单）
        """
        if not data.get('Success'):
            msg = data.get('Message') or f"ErrorCode: {data.get('ErrorCode', '')}"
            raise ValueError(f"接口返回失败: {msg}")
        
        payload = data.get('Data')
        # Success=true 且 Data 为空：通常表示当前无待处理订单
        if payload is None or payload == "" or payload == {} or payload == []:
            self.logger.info("接口 Data 为空，视为当前无待处理订单")
            return []
        
        # 单条订单：Data 为对象，含 OrderId 和 List
        if isinstance(payload, dict) and 'OrderId' in payload and 'List' in payload:
            order = self._parse_formal_api_order(payload)
            return [order]
        
        # 若接口返回多个订单：Data 为数组
        if isinstance(payload, list):
            orders = []
            for item in payload:
                if isinstance(item, dict) and 'OrderId' in item and 'List' in item:
                    orders.append(self._parse_formal_api_order(item))
            return orders
        
        raise ValueError("无法识别的 Data 格式")
    
    def _parse_formal_api_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        将正式接口 Data 单条（OrderId、UserId、List 等）转为内部订单结构。
        Data 含: OrderId, UserId, List, Mark, Secret；
        List 项: GoodsId, GoodsNo, GoodsUrl, GoodsNumber, GoodsPrice。
        同时为每个商品打上「门店ID / 是否第三方店铺」标记，供后续支付方式选择使用。
        """
        order_id = str(data.get('OrderId', ''))
        # 接口返回的 OrderNo 用于 is_success.log 等（如 RS26031211021）
        order_no_raw = data.get('OrderNo')
        order_no = str(order_no_raw).strip() if order_no_raw is not None else order_id
        # 接口返回的 UserId 供工单使用（如 274572）
        user_id_raw = data.get('UserId')
        user_id = str(user_id_raw) if user_id_raw is not None else None
        goods_list = data.get('List') or []
        
        order = {
            'order_id': order_id,
            'order_no': order_no,
            'user_id': user_id,
            'status': '已支付',
            'platform': '骏河屋',
            'products': [],
            'mark': data.get('Mark'),
            'secret': data.get('Secret'),
        }
        
        for item in goods_list:
            url = item.get('GoodsUrl', '')
            price = float(item.get('GoodsPrice', 0) or 0)
            quantity = int(item.get('GoodsNumber', 0) or 0)
            name = str(item.get('GoodsNo', '') or '').strip() or '未知商品'
            if not name and url:
                try:
                    parts = url.rstrip('/').split('/')
                    if parts:
                        name = parts[-1].split('?')[0]
                except Exception:
                    pass

            # 第三方店铺标记：URL 中若包含 tenpo_cd（如 ?tenpo_cd=400546），则视为第三方店铺
            shop_id: Optional[str] = None
            is_third_party = False
            if url:
                try:
                    parsed = urlparse(url)
                    qs = parse_qs(parsed.query)
                    tenpo_vals = qs.get('tenpo_cd') or qs.get('tenpo_cd[]') or []
                    if tenpo_vals:
                        shop_id = str(tenpo_vals[0])
                        is_third_party = True
                except Exception:
                    pass
            
            goods_id = item.get('GoodsId')
            goods_no = item.get('GoodsNo', '') or ''
            if goods_id is not None:
                goods_id = str(goods_id).strip()
            else:
                goods_id = ''
            goods_no = str(goods_no).strip() if goods_no else ''

            product: Dict[str, Any] = {
                'name': name or '未知商品',
                'url': url,
                'price': price,
                'quantity': quantity,
                'goods_id': goods_id,
                'goods_no': goods_no,
            }
            # 为后续支付逻辑预留标记字段
            product['shop_id'] = shop_id
            product['is_third_party'] = is_third_party
            # 简单的类型标记：0 = 官方，1 = 第三方店铺
            product['shop_type'] = 1 if is_third_party else 0
            
            order['products'].append(product)
        
        return order
    
    def _parse_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析单个订单数据（兼容旧格式，新格式请用 _parse_formal_api_order）
        """
        return order_data

