"""吉客云 API 封装 — 走 hermes-web-api 网关路由（localhost:8088）"""

from typing import Any, Optional

import httpx

from ..config import load_settings


class JkyClient:
    """吉客云 API 客户端（通过 hermes-web-api 网关代理）"""

    def __init__(self, gateway_url: str, api_key: str):
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=20, write=20, pool=5))

    async def _post(self, path: str, biz: dict) -> dict:
        url = f"{self.gateway_url}{path}"
        resp = await self._client.post(
            url,
            json=biz,
            params={"api_key": self.api_key},
        )
        resp.raise_for_status()
        return resp.json()

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.gateway_url}{path}"
        query = dict(params or {})
        query["api_key"] = self.api_key
        resp = await self._client.get(url, params=query)
        resp.raise_for_status()
        return resp.json()

    # ---- 销售单 ----

    async def trade_create(self, biz: dict) -> dict:
        """创建销售单"""
        return await self._post("/jky/trade/create", biz)

    async def trade_audit(self, biz: dict) -> dict:
        """审核销售单"""
        return await self._post("/jky/trade/audit", biz)

    async def trade_cancel(self, biz: dict) -> dict:
        """取消销售单"""
        return await self._post("/jky/trade/cancel", biz)

    async def trade_list(self, biz: dict) -> dict:
        """查询销售单列表"""
        return await self._post("/jky/trade/list", biz)

    # ---- 货品 ----

    async def goods_search(self, biz: dict) -> dict:
        """搜索货品"""
        return await self._post("/jky/goods/list", biz)

    async def logistic_list(self, biz: dict) -> dict:
        """查询物流公司列表（cron-e / scope 3 bootstrap）"""
        return await self._post("/jky/logistic/list", biz)

    async def close(self):
        await self._client.aclose()


# ---------- Factory ----------

def create_jky_client(settings: dict) -> JkyClient:
    """从 settings 字典创建客户端"""
    cfg = settings.get("jky", {})
    creds = settings.get("_credentials", {}).get("jky_gateway", {})
    return JkyClient(
        gateway_url=cfg.get("gateway_url", "http://localhost:8088"),
        api_key=creds.get("api_key", ""),
    )
