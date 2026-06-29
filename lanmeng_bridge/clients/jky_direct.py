"""吉客云 API 直连客户端（不走 web-api 网关）"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("lanmonshop-bridge.jky_direct")

API_URL = "https://open.jackyun.com/open/openapi/do"
APPKEY = "83311133"
APPSECRET = "48c5316d29d745cc9db0bd79fdc20d34"


def _sign(params: dict) -> str:
    """吉客云签名算法：md5(appSecret + concat_sorted_kv + appSecret)"""
    excluded = {"sign", "token", "contextid"}
    filtered = {k: v for k, v in params.items() if k not in excluded and v is not None}
    concat = "".join(f"{k}{filtered[k]}" for k in sorted(filtered))
    raw = f"{APPSECRET}{concat}{APPSECRET}".lower()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _build_params(method: str, bizcontent: dict) -> dict:
    """构造吉客云开放平台请求参数"""
    return {
        "method": method,
        "appkey": APPKEY,
        "version": "v1.0",
        "contenttype": "json",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bizcontent": json.dumps(bizcontent, ensure_ascii=False, separators=(",", ":")),
    }


def _build_signed_params(method: str, bizcontent: dict) -> dict:
    params = _build_params(method, bizcontent)
    params["sign"] = _sign(params)
    return params


class JkyDirectClient:
    """吉客云开放平台直连客户端"""

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=5),
        )

    async def _call(self, method: str, bizcontent: dict) -> dict:
        params = _build_signed_params(method, bizcontent)
        logger.info(f"[jky_direct] {method} → size={len(json.dumps(bizcontent))}b")
        resp = await self._client.post(API_URL, data=params)
        resp.raise_for_status()
        result = resp.json()
        logger.info(f"[jky_direct] {method} ← code={result.get('code')} subCode={result.get('subCode', '')}")
        return result

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
def create_jky_direct_client() -> JkyDirectClient:
    return JkyDirectClient()
