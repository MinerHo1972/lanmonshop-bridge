"""飞书告警 — P0/P1/P2 三套模板"""

import json
from typing import Optional

import httpx


class FeishuNotifier:
    """飞书群机器人告警"""

    def __init__(self, webhook_url: str, p0_at_all: bool = True):
        self.webhook_url = webhook_url
        self.p0_at_all = p0_at_all
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(5))

    async def _send(self, content: str):
        payload = {
            "msg_type": "text",
            "content": {"text": content},
        }
        try:
            resp = await self._client.post(self.webhook_url, json=payload)
            resp.raise_for_status()
        except Exception:
            pass  # 告警本身失败不阻塞主流程

    async def alert_p0(self, order_no: str, summary: str,
                       order_map_id: int, state: str):
        """P0 资损风险 — 立即人工"""
        text = (
            f"🚨 [P0 资损风险]\n"
            f"单号: {order_no}\n"
            f"现象: {summary}\n"
            f"订单: order_map.id={order_map_id}\n"
            f"当前状态: {state}\n"
            f"操作: 立即查 order_map → 决定手动干预"
        )
        if self.p0_at_all:
            text += "\n@all"
        await self._send(text)

    async def alert_p1(self, order_no: str, last_error: str,
                       retry_count: int, order_map_id: int):
        """P1 单订单卡住 — 30min 内处理"""
        text = (
            f"⚠️ [P1 单订单卡住]\n"
            f"单号: {order_no}\n"
            f"失败原因: {last_error}\n"
            f"已重试: {retry_count}/3\n"
            f"订单: order_map.id={order_map_id}\n"
            f"SQL: SELECT * FROM order_map WHERE id={order_map_id};\n"
            f"状态日志: SELECT * FROM order_status_log WHERE "
            f"order_map_id={order_map_id} ORDER BY ts DESC LIMIT 10;"
        )
        await self._send(text)

    async def alert_p2_upgrade(self, error_type: str, error_detail: str,
                                count: int, affected: int):
        """P2 → P1 升级告警"""
        text = (
            f"🔁 [P2→P1 升级] {error_type}\n"
            f"累计次数: {count}/3\n"
            f"详情: {error_detail}\n"
            f"影响范围: {affected} 单"
        )
        await self._send(text)

    async def close(self):
        await self._client.aclose()
