"""蓝盟中台 API 封装 — 鉴权签名 + 3 个接口"""

import hashlib
import time
from typing import Any, Optional

import httpx

from ..config import load_settings

# ---------- 签名算法 ----------
# 算法：md5(appKey & timestamp & appSecret).toUpperCase()


def _sign(app_key: str, app_secret: str, timestamp: str) -> str:
    raw = app_key + timestamp + app_secret
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
        timestamp = str(int(time.time() * 1000))
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
        page_no: int = 1,
        page_size: int = 50,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        state: int = 1,
    ) -> dict:
        """拉取待发货订单

        POST /open/v1/order/getDeliverOrders
        state=1 = 已支付待审核/待发货
        """
        body = {
            "pageNo": page_no,
            "pageSize": page_size,
            "state": state,
        }
        if start_time:
            body["startTime"] = start_time
        if end_time:
            body["endTime"] = end_time
        return await self._post("/open/v1/order/getDeliverOrders", body)

    async def review_order(self, order_id: int) -> dict:
        """审核订单（自动过审）

        POST /open/v1/order/reviewOrder
        """
        return await self._post("/open/v1/order/reviewOrder", {"orderId": order_id})

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
        items = [{skuNo, num}, ...]
        """
        body = {
            "orderId": order_id,
            "orderNo": order_no,
            "expressNo": express_no,
            "expressCode": express_code,
            "expressName": express_name,
            "warehouseId": warehouse_id,
            "warehouseName": warehouse_name,
            "orderItems": items,
        }
        return await self._post("/open/v1/order/syncOrderExpress", body)

    async def close(self):
        await self._client.aclose()


# ---------- Factory ----------

def create_lanmong_client(settings: dict) -> LanmongClient:
    """从 settings 字典创建客户端"""
    cfg = settings.get("lanmong", {})
    creds = settings.get("_credentials", {}).get("lanmong", {})
    return LanmongClient(
        base_url=cfg.get("base_url", "https://test-zt-api.lanmonshop.com"),
        app_key=creds.get("app_key", ""),
        app_secret=creds.get("app_secret", ""),
    )
