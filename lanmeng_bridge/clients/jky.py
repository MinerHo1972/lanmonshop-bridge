"""吉客云 API 封装 — 走 hermes-web-api 网关路由（localhost:8088）"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from ..config import load_settings
from ..storage.db import log_api_call


class JkyClient:
    """吉客云 API 客户端（通过 hermes-web-api 网关代理）"""

    def __init__(self, gateway_url: str, api_key: str):
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=20, write=20, pool=5))

    async def _post(self, path: str, biz: dict) -> dict:
        url = f"{self.gateway_url}{path}"
        req_body = json.dumps(biz, ensure_ascii=False, default=str)
        logger = logging.getLogger("lanmonshop-bridge.jky")
        logger.info(f"[jky] → POST {path} body={req_body[:800]}")
        logger.debug(f"[jky] → POST {path} full_body={req_body}")
        t0 = datetime.now()
        error_msg = ""
        http_status = 0
        api_code = 0
        api_sub_code = ""
        resp_body = ""
        try:
            resp = await self._client.post(
                url,
                json=biz,
                params={"api_key": self.api_key},
            )
            resp.raise_for_status()
            result = resp.json()
            http_status = resp.status_code
            api_code = result.get("code", 0)
            api_sub_code = result.get("subCode", "") or ""
            resp_body = json.dumps(result, ensure_ascii=False, default=str)
            logger.info(f"[jky] ← POST {path} status={http_status} body={resp_body[:800]}")
            logger.debug(f"[jky] ← POST {path} full_body={resp_body}")
            return result
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[jky] ✗ POST {path} error={error_msg}")
            raise
        finally:
            duration = int((datetime.now() - t0).total_seconds() * 1000)
            log_api_call(
                source="jky_gateway",
                method=f"POST {path}",
                request_body=req_body,
                response_body=resp_body,
                http_status=http_status,
                api_code=api_code,
                api_sub_code=api_sub_code,
                error=error_msg,
                duration_ms=duration,
            )

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
