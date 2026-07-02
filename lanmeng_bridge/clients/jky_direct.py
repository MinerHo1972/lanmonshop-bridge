"""吉客云 API 直连客户端（不走 web-api 网关）"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from ..storage.db import log_api_call

logger = logging.getLogger("lanmonshop-bridge.jky_direct")

API_URL = "https://open.jackyun.com/open/openapi/do"
# fallback 值（优先从 credentials.yaml → jky_direct 读取）
APPKEY_FALLBACK = "83311133"
APPSECRET_FALLBACK = "48c5316d29d745cc9db0bd79fdc20d34"


class JkyDirectClient:
    """吉客云开放平台直连客户端"""

    def __init__(self, appkey: str, app_secret: str):
        self.appkey = appkey
        self.app_secret = app_secret
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=5),
        )

    async def _call(self, method: str, bizcontent: dict) -> dict:
        params = self._build_signed_params(method, bizcontent)
        req_body = json.dumps(bizcontent, ensure_ascii=False, default=str)
        logger.info(f"[jky_direct] → {method} body={req_body[:800]}")
        logger.debug(f"[jky_direct] → {method} full_body={req_body}")
        t0 = datetime.now()
        error_msg = ""
        http_status = 0
        api_code = 0
        api_sub_code = ""
        resp_body = ""
        try:
            resp = await self._client.post(API_URL, data=params)
            resp.raise_for_status()
            result = resp.json()
            http_status = resp.status_code
            api_code = result.get("code")
            api_sub_code = result.get("subCode", "") or ""
            resp_body = json.dumps(result, ensure_ascii=False, default=str)
            logger.info(f"[jky_direct] ← {method} code={api_code} subCode={api_sub_code} body={resp_body[:800]}")
            logger.debug(f"[jky_direct] ← {method} full_body={resp_body}")
            return result
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[jky_direct] ✗ {method} error={error_msg}")
            raise
        finally:
            duration = int((datetime.now() - t0).total_seconds() * 1000)
            log_api_call(
                source="jky_direct",
                method=method,
                request_body=req_body,
                response_body=resp_body,
                http_status=http_status,
                api_code=api_code,
                api_sub_code=api_sub_code,
                error=error_msg,
                duration_ms=duration,
            )

    def _sign(self, params: dict) -> str:
        """吉客云签名算法：md5(appSecret + concat_sorted_kv + appSecret)"""
        excluded = {"sign", "token", "contextid"}
        filtered = {k: v for k, v in params.items() if k not in excluded and v is not None}
        concat = "".join(f"{k}{filtered[k]}" for k in sorted(filtered))
        raw = f"{self.app_secret}{concat}{self.app_secret}".lower()
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _build_params(self, method: str, bizcontent: dict) -> dict:
        """构造吉客云开放平台请求参数"""
        return {
            "method": method,
            "appkey": self.appkey,
            "version": "v1.0",
            "contenttype": "json",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bizcontent": json.dumps(bizcontent, ensure_ascii=False, separators=(",", ":")),
        }

    def _build_signed_params(self, method: str, bizcontent: dict) -> dict:
        params = self._build_params(method, bizcontent)
        params["sign"] = self._sign(params)
        return params

    # ---------- 销售单 ----------

    async def trade_create(self, trade_order: dict) -> dict:
        """创建销售单"""
        return await self._call("oms.trade.ordercreate", {"tradeOrder": trade_order})

    async def trade_audit(self, trade_ids: str) -> dict:
        """审核销售单"""
        return await self._call("oms.trade.audit.pass", {"tradeIds": trade_ids})

    async def trade_cancel(self, trade_nos: str, cancel_reason: str = "420001") -> dict:
        """取消销售单 (字段名 tradeNos, 不是 tradeIds)"""
        return await self._call("oms.trade.ordercancel", {
            "tradeNos": trade_nos,
            "cancelReason": cancel_reason,
        })

    async def trade_list(self, biz: dict) -> dict:
        """查询销售单列表（oms.trade.fullinfoget.customized）"""
        # customized API 必传 fields（否则报 0040139996 查询字段不能为空）
        if "fields" not in biz:
            biz["fields"] = "tradeNo,onlineTradeNo,shopName,shopId,tradeTime,consignTime,signingTime,warehouseName,warehouseCode,payment,logisticName,mainPostid,tradeStatus,tradeStatusExplain"
        return await self._call("oms.trade.fullinfoget.customized", biz)

    # ---------- 货品 ----------

    async def goods_search(self, biz: dict) -> dict:
        """搜索货品（erp-goods.goods.sku.search）"""
        return await self._call("erp-goods.goods.sku.search", biz)

    # ---------- 物流 ----------

    async def logistic_list(self, biz: dict) -> dict:
        """查询物流公司列表（erp.logistic.get）"""
        return await self._call("erp.logistic.get", biz)

    async def close(self):
        await self._client.aclose()


# 工厂函数
def create_jky_direct_client(settings: dict) -> JkyDirectClient:
    """从 settings 读取凭证创建直连客户端"""
    creds = settings.get("_credentials", {}).get("jky_direct", {})
    appkey = creds.get("appkey", "") or APPKEY_FALLBACK
    app_secret = creds.get("app_secret", "") or APPSECRET_FALLBACK
    return JkyDirectClient(appkey=appkey, app_secret=app_secret)
