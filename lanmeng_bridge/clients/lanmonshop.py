"""蓝盟中台 API 封装 — 鉴权签名 + 3 个接口"""

import hashlib
import time
from typing import Any, Optional

import httpx

from ..config import load_settings

# ---------- 签名算法 ----------
# 算法（来自中台对外开放接口规范 2026-06-22 PDF）：
# sign = md5(appKey & timestamp & appSecret).toUpperCase()
# timestamp = 秒级（不是毫秒！）
# 参考：docs/中台对外开放接口规范-蓝盟-20260622.pdf 第2页


def _sign(app_key: str, app_secret: str, timestamp: str) -> str:
    raw = f"{app_key}&{timestamp}&{app_secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


class LanmongClient:
    """蓝盟中台 API 客户端"""

    def __init__(self, base_url: str, app_key: str, app_secret: str):
        self.base_url = base_url.rstrip("/")
        self.app_key = app_key
        self.app_secret = app_secret
        # httpx >= 0.28 requires either default= or all 4 params explicitly
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=20, write=20, pool=5))

    def _headers(self) -> dict:
        timestamp = str(int(time.time()))  # 秒级（非毫秒！文档原文）
        sign = _sign(self.app_key, self.app_secret, timestamp)
        return {
            "appKey": self.app_key,
            "sign": sign,
            "timestamp": timestamp,
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        resp = await self._client.post(url, json=body, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    # ---- 接口 ----

    async def get_deliver_orders(
        self,
        page_num: int = 1,
        page_size: int = 50,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
        pay_time_start: Optional[str] = None,
        pay_time_end: Optional[str] = None,
        supplier_update_time_start: Optional[str] = None,
        supplier_update_time_end: Optional[str] = None,
        order_no: Optional[str] = None,
        state: str = "1",
    ) -> dict:
        """拉取待发货订单

        POST /open/v1/order/getDeliverOrders
        文档 P44: 请求参数 pageNum/pageSize/timeStart/timeEnd/payTimeStart/payTimeEnd/state
        state: "1"=已支付待审核, "2"=待发货, "4"=已发货, "-2"=已取消
        """
        body = {
            "pageNum": page_num,
            "pageSize": page_size,
        }
        if time_start:
            body["timeStart"] = time_start
        if time_end:
            body["timeEnd"] = time_end
        if pay_time_start:
            body["payTimeStart"] = pay_time_start
        if pay_time_end:
            body["payTimeEnd"] = pay_time_end
        if supplier_update_time_start:
            body["supplierUpdateTimeStart"] = supplier_update_time_start
        if supplier_update_time_end:
            body["supplierUpdateTimeEnd"] = supplier_update_time_end
        if order_no:
            body["orderNo"] = order_no
        if state:
            body["state"] = state
        return await self._post("/open/v1/order/getDeliverOrders", body)

    async def review_order(self, order_no: str, result: int = 0, reason: Optional[str] = None) -> dict:
        """审核订单（自动过审）

        POST /open/v1/order/reviewOrder
        文档 P48-49: {list: [{orderNo, result, reason?}]}
        result: 0=通过, -1=驳回取消
        """
        body = {"list": [{"orderNo": order_no, "result": result}]}
        if reason:
            body["list"][0]["reason"] = reason
        return await self._post("/open/v1/order/reviewOrder", body)

    async def sync_order_express(
        self,
        order_id: int,
        order_no: str,
        express_no: str,
        express_code: str,
        express_name: str,
        warehouse_id: int,
        warehouse_name: str,
        items: list[dict],
    ) -> dict:
        """回传物流单号

        POST /open/v1/order/syncOrderExpress
        文档 P50-52: list: [{orderId/orderNo, warehouseId, warehouseName,
                          expressName/expressCode, expressNo,
                          orderItems: [{orderItemId, num}]}]
        items 格式: [{orderItemId, num}]
        """
        body = {
            "list": [{
                "orderId": order_id,
                "orderNo": order_no,
                "expressNo": express_no,
                "expressCode": express_code,
                "expressName": express_name,
                "warehouseId": warehouse_id,
                "warehouseName": warehouse_name,
                "orderItems": items,
            }]
        }
        return await self._post("/open/v1/order/syncOrderExpress", body)

    async def close(self):
        await self._client.aclose()


# ---------- Factory ----------

def create_lanmong_client(settings: dict) -> LanmongClient:
    """从 settings 字典创建客户端"""
    cfg = settings.get("lanmong", {})
    # credentials.yaml 嵌套路径: credentials.lanmenshop
    # config.py 将整个 yaml 注入到 _credentials，所以取 creds.credentials.lanmenshop
    creds = (
        settings.get("_credentials", {})
        .get("credentials", {})
        .get("lanmenshop", {})
    )
    return LanmongClient(
        base_url=cfg.get("base_url", "https://test-zt-api.lanmonshop.com"),
        app_key=creds.get("appkey", ""),   # 文档字段名 appKey, yaml 存为 appkey
        app_secret=creds.get("secret", ""),
    )
