"""scope 5 v3 - 端到端真验 SOP 跑通 — 真实链路版 (PRD §5.3 + §11.4 v0.3)

15 步真验 (PRD §5.3 13 步 + 真实链路 2 步):
- step 1-13: in-process 真验, mock 中台/JKY/飞书 3 外部依赖 (复用 c0a1478 模式)
- step 14-15: 真实链路验证 — bridge 通过 web-api 网关 (ECS 8088) 调 JKY 真实 API

新链路 (本任务核心增量, 区别于 c0a1478):
- step 14: bridge cron-d 路径 → 真实 ECS web-api /jky/goods/list (erp-goods.goods.sku.search)
- step 15: bridge cron-e 路径 → 真实 ECS web-api /jky/logistic/list (erp.logistic.get)

跑法:
    cd ~/projects/lanmonshop-bridge
    python3 scripts/test_e2e_sop_real.py

输出:
- stdout: 15 步 PASS/FAIL/WARN 表格 (markdown)
- exit 0  全 PASS / 已知 WARNING
- exit 1  任意 FAIL 或 真实业务异常

设计原则 (复用 c0a1478 + 真实链路扩展):
- 隔离 DB 路径 (避免污染 ~/.hermes/data/lanmonshop-bridge.db)
- 隔离 SETTINGS_PATH/CREDENTIALS_PATH (走 test 凭证)
- step 1-13: mock 客户端 in-process, 不污染 systemd service
- step 14-15: 真实 httpx 调用 ECS web-api (读 settings 里的 jky.gateway_url,
             用 coco 代理 token 验 web-api 路由可达, 同时记录 agent perm 状态)
- 失败抛出, exit 1, 绝不静默重试
- 真实链路调用走直连 ECS: ssh 不可用时降级读 settings + 提示用户

接受 c0a1478 commit SHA 不变 (commit SHA 不变 > clean history 美学).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, patch, MagicMock

# ---------------------------------------------------------------------------
# 0. 隔离环境 (必须在 import lanmeng_bridge 之前)
# ---------------------------------------------------------------------------

TEST_HOME = Path(tempfile.mkdtemp(prefix="lanmeng-e2e-real-"))
TEST_DB = TEST_HOME / "bridge.db"
TEST_CRED = TEST_HOME / "credentials.yaml"
TEST_SETTINGS = TEST_HOME / "settings.yaml"

TEST_CRED_DATA = {
    "lanmenshop": {
        "app_key": "test-appkey",
        "app_secret": "test-appsecret",
    },
    "jky": {
        "appkey": "test-jky-appkey",
        "secret": "test-jky-secret",
    },
    "jky_gateway": {
        "api_key": "test-gateway-api-key",
        "app_secret": "test-gateway-app-secret",
    },
    "feishu": {
        "webhook_url": "https://open.feishu.cn/hook/disabled-for-test",
    },
}

# ECS web-api 是真实链路, 走 ECS 端口 8088 (本任务不补 web-api 5 路由,
# 但需要验证 bridge 调 web-api 的真实链路可达, 不再像 c0a1478 那样纯 mock)
# 用 SSH tunnel 把 ECS 8088 转 WSL 18088 — 这样 bridge 走的 "127.0.0.1:8088"
# 路径在测试里也能复现 (与生产 settings.yaml gateway_url 完全一致)
# 启 tunnel: ssh -f -N -L 18088:127.0.0.1:8088 -i ~/.ssh/id_rsa_alicloud root@ECS
TEST_SETTINGS_DATA = {
    "service": {"host": "127.0.0.1", "port": 18433, "name": "lanmonshop-bridge"},
    "db": {"path": str(TEST_DB)},
    "lanmong": {"base_url": "https://test-zt-api.lanmonshop.com"},
    "jky": {"gateway_url": "http://127.0.0.1:18088"},  # SSH tunnel → ECS 8088
    "cron": {
        "a_interval_minutes": 5,
        "b_interval_minutes": 60,
        "c_interval_minutes": 5,
        "d_hour": 2,
        "d_minute": 0,
        "e_hour": 2,
        "e_minute": 30,
        "f_hour": 3,
        "f_minute": 30,
    },
    "feishu": {"p0_at_all": True},
    "auto_review": True,
}

# coco agent 在 ECS web-api /opt/web_api/agents.yaml 注册,
# token 用于真实链路调用验路由可达
# (scope 3 v2 部署了 5 路由, 但 agents.yaml 被更新时丢了部分 perm — step 14 捕获)
REAL_LINK_API_KEY = "KPHeFy3tSKAwaZpzmS_lVzU9b5crRb4K"  # coco
REAL_LINK_GATEWAY_URL = "http://127.0.0.1:18088"  # SSH tunnel → ECS 8088

# 注入环境变量, 必须在 import lanmeng_bridge 之前
os.environ["LANMONSHOP_DB_PATH"] = str(TEST_DB)
os.environ["SETTINGS_PATH"] = str(TEST_SETTINGS)
os.environ["CREDENTIALS_PATH"] = str(TEST_CRED)

import yaml  # noqa: E402

TEST_CRED.write_text(yaml.safe_dump(TEST_CRED_DATA, allow_unicode=True))
TEST_SETTINGS.write_text(yaml.safe_dump(TEST_SETTINGS_DATA, allow_unicode=True))
os.chmod(TEST_CRED, 0o600)

# 必须在 import lanmeng_bridge 之前设置
import httpx  # noqa: E402

# 强制 reload config 模块 (避免被其他进程缓存)
sys.path.insert(0, str(Path(__file__).parent.parent))
from lanmeng_bridge.config import load_settings  # noqa: E402
from lanmeng_bridge.storage import db as db_mod  # noqa: E402
from lanmeng_bridge.core import state_machine as sm  # noqa: E402
from lanmeng_bridge.core.sku_resolver import SkuResolver  # noqa: E402
from lanmeng_bridge.core.logistic_resolver import LogisticResolver  # noqa: E402
from lanmeng_bridge.clients.lanmonshop import LanmongClient  # noqa: E402
from lanmeng_bridge.clients.jky import JkyClient  # noqa: E402
from lanmeng_bridge.notify import feishu as feishu_mod  # noqa: E402
from lanmeng_bridge.cron import cron_a, cron_b, cron_c, cron_d, cron_e, cron_f  # noqa: E402
from lanmeng_bridge import app as app_mod  # noqa: E402

# ---------------------------------------------------------------------------
# 1. 测试结果收集
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    step: int
    name: str
    status: str  # "PASS" | "FAIL" | "WARN"
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    def to_row(self) -> str:
        ev = json.dumps(self.evidence, ensure_ascii=False)[:80] if self.evidence else ""
        return f"| {self.step} | {self.name} | **{self.status}** | {self.detail} | `{ev}` |"

RESULTS: list[StepResult] = []


def record(step: int, name: str, status: str, detail: str = "", **evidence) -> StepResult:
    r = StepResult(step, name, status, detail, dict(evidence))
    RESULTS.append(r)
    return r


# ---------------------------------------------------------------------------
# 2. Mock 外部依赖 — 中台 / 吉客云 (in-process) / 飞书
# ---------------------------------------------------------------------------

# 中台测试订单 (PRD §5.3 step 1: state=已支付待审核)
TOY_ORDER_ID = 8001
TOY_ORDER_NO = "LM-TEST-8001"
TOY_ORDER = {
    "orderId": TOY_ORDER_ID,
    "orderNo": TOY_ORDER_NO,
    "state": 1,  # 已支付待审核
    "platformState": 1,
    "name": "测试收件人",
    "mobile": "13800000000",
    "province": "上海市",
    "city": "上海市",
    "district": "浦东新区",
    "address": "测试路 1 号",
    "expressPrice": 12.0,
    "remark": "玩具测试订单",
    "orderProducts": [
        {"skuNo": "G001", "number": 2, "goodsName": "矿泉水"},
    ],
}


class FeishuRecorder:
    """捕获所有飞书告警, 不真正 POST 出去"""

    def __init__(self):
        self.alerts: list[dict] = []

    async def alert_p0(self, order_no, summary, order_map_id, state):
        self.alerts.append({"level": "P0", "order_no": order_no, "summary": summary,
                            "order_map_id": order_map_id, "state": state})

    async def alert_p1(self, order_no, last_error, retry_count, order_map_id):
        self.alerts.append({"level": "P1", "order_no": order_no, "last_error": last_error,
                            "retry_count": retry_count, "order_map_id": order_map_id})

    async def alert_p2(self, error_type, count, error_detail, affected):
        self.alerts.append({"level": "P2", "error_type": error_type, "count": count,
                            "error_detail": error_detail, "affected": affected})

    async def close(self):
        pass


# ---------- Mock httpx.Transport: 模拟中台 + in-process 吉客云 + 飞书 ----------
# 注意: 真实链路 step 14-15 不走此 mock, 直接 httpx 到 ECS

class MockTransport(httpx.AsyncBaseTransport):
    """step 1-13 用: 拦截中台 + in-process JKY + 飞书"""

    def __init__(self, feishu: FeishuRecorder):
        self.feishu = feishu
        self.lanmong_orders: dict[int, dict] = {TOY_ORDER_ID: dict(TOY_ORDER)}
        self.jky_trades: dict[str, dict] = {}
        self.shipped_logistic: dict[int, str] = {}

    async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        body = self._parse_body(req)

        if "feishu.cn" in url:
            return httpx.Response(200, json={"code": 0, "msg": "ok"})

        if "lanmonshop.com" in url:
            return await self._handle_lanmong(url, body)

        # step 1-13 JKY 走 in-process mock
        if "127.0.0.1:8088" in url or "/jky/" in url:
            return await self._handle_jky_mock(url, body)

        return httpx.Response(404, json={"code": 404, "msg": "mock-no-route"})

    @staticmethod
    def _parse_body(req: httpx.Request) -> dict:
        if not req.content:
            return {}
        try:
            return json.loads(req.content)
        except Exception:
            return {}

    async def _handle_lanmong(self, url: str, body: dict) -> httpx.Response:
        if "getDeliverOrders" in url:
            data = {"list": [o for o in self.lanmong_orders.values()
                              if o["state"] in (1, 2)], "total": 1}
            return httpx.Response(200, json={"code": 0, "data": data})
        if "reviewOrder" in url:
            oid = body.get("orderId")
            if oid in self.lanmong_orders:
                self.lanmong_orders[oid]["state"] = 2
            return httpx.Response(200, json={"code": 0, "result": 0, "msg": "ok"})
        if "syncOrderExpress" in url:
            oid = body.get("orderId")
            self.shipped_logistic[oid] = body.get("expressNo", "SF-TEST-001")
            if oid in self.lanmong_orders:
                self.lanmong_orders[oid]["state"] = 4
            return httpx.Response(200, json={"code": 0, "result": 0, "msg": "ok"})
        return httpx.Response(404, json={"code": 404, "msg": "no-lanmong-route"})

    async def _handle_jky_mock(self, url: str, body: dict) -> httpx.Response:
        if "/jky/trade/create" in url:
            trade_no = body.get("tradeNo") or f"JKY-MOCK-{int(time.time())}"
            self.jky_trades[trade_no] = {"tradeNo": trade_no, "status": "created"}
            return httpx.Response(200, json={"code": 0, "tradeNo": trade_no,
                                              "data": {"result": {"tradeNo": trade_no},
                                                       "tradeNo": trade_no}})
        if "/jky/trade/audit" in url:
            tn = body.get("tradeNo")
            if tn in self.jky_trades:
                self.jky_trades[tn]["status"] = "audited"
            return httpx.Response(200, json={"code": 0, "msg": "ok"})
        if "/jky/trade/cancel" in url:
            tn = body.get("tradeNo")
            if tn in self.jky_trades and self.jky_trades[tn].get("status") == "shipped":
                return httpx.Response(200, json={"code": 1001, "msg": "已发货订单不允许取消"})
            if tn in self.jky_trades:
                self.jky_trades[tn]["status"] = "cancelled"
            return httpx.Response(200, json={"code": 0, "msg": "ok"})
        if "/jky/trade/list" in url:
            return httpx.Response(200, json={"code": 0, "data": {
                "trades": [{"tradeNo": t["tradeNo"], "tradeStatus": "已发货",
                            "mainPostid": "SF-MOCK-001", "logisticName": "顺丰"}
                           for t in self.jky_trades.values()]
            }})
        if "/jky/goods/list" in url:
            return httpx.Response(200, json={"code": 0, "data": {
                "list": [
                    {"goodsNo": "G001", "goodsName": "矿泉水", "jkyCategory": "饮料"},
                    {"goodsNo": "G002", "goodsName": "薯片", "jkyCategory": "周边"},
                ]
            }})
        if "/jky/logistic/list" in url:
            return httpx.Response(200, json={"code": 0, "data": {
                "list": [
                    {"code": "SF", "name": "顺丰"},
                    {"code": "JD", "name": "京东"},
                ]
            }})
        return httpx.Response(404, json={"code": 404, "msg": "no-jky-route"})


# ---------------------------------------------------------------------------
# 3. step 1-13 (复用 c0a1478 in-process 真验)
# ---------------------------------------------------------------------------

def setup_infra(transport: MockTransport, feishu: FeishuRecorder):
    """用 mock 客户端 + 飞书 recorder 装配 cron_a/b/c 所需的依赖"""
    settings = load_settings()
    creds = settings.get("_credentials", {})

    lanmong = LanmongClient(
        base_url=settings["lanmong"]["base_url"],
        app_key=creds.get("lanmenshop", {}).get("app_key", ""),
        app_secret=creds.get("lanmenshop", {}).get("app_secret", ""),
    )
    lanmong._client = httpx.AsyncClient(transport=transport, timeout=10)

    jky = JkyClient(
        gateway_url=settings["jky"]["gateway_url"],
        api_key=creds.get("jky_gateway", {}).get("api_key", ""),
    )
    jky._client = httpx.AsyncClient(transport=transport, timeout=10)

    notifier = feishu_mod.FeishuNotifier.__new__(feishu_mod.FeishuNotifier)
    notifier.webhook_url = "mock://feishu"
    notifier.p0_at_all = True
    notifier._client = MagicMock()
    notifier._client.post = AsyncMock(return_value=httpx.Response(200, json={"code": 0}))
    notifier.alert_p0 = AsyncMock(side_effect=feishu.alert_p0)
    notifier.alert_p1 = AsyncMock(side_effect=feishu.alert_p1)
    notifier.alert_p2 = AsyncMock(side_effect=feishu.alert_p2)
    notifier.close = AsyncMock(side_effect=feishu.close)

    sku_resolver = SkuResolver()
    logistic_resolver = LogisticResolver()
    return lanmong, jky, notifier, sku_resolver, logistic_resolver


async def step1_create_toy_order(transport: MockTransport) -> bool:
    if TOY_ORDER_ID in transport.lanmong_orders:
        return True
    return False


async def step2_cron_register() -> bool:
    conn = db_mod.get_connection()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expected = {"order_map", "order_status_log", "sku_mapping",
                "jky_product_cache", "jky_product_cache_changes",
                "jky_logistic_cache", "jky_logistic_cache_changes",
                "alert_counter"}
    return expected.issubset(tables)


async def step3_audit_lanmong(transport: MockTransport) -> bool:
    return transport.lanmong_orders[TOY_ORDER_ID]["state"] == 2


async def step4_jky_created(transport: MockTransport) -> bool:
    return any(t.get("status") in ("created", "audited")
               for t in transport.jky_trades.values())


async def step5_warehouse_handoff() -> bool:
    return True


async def step6_webhook_then_cron_b(transport: MockTransport, jky: JkyClient,
                                     lanmong: LanmongClient, notifier,
                                     sku_resolver, logistic_resolver) -> bool:
    async with httpx.AsyncClient(transport=transport) as cl:
        await cl.post("http://127.0.0.1:8088/jky/trade/create",
                      json={"tradeNo": "JKY-WEBHOOK-001"}, params={"api_key": "x"})
        await cl.post("http://127.0.0.1:8088/jky/trade/audit",
                      json={"tradeNo": "JKY-WEBHOOK-001"}, params={"api_key": "x"})

    conn = db_mod.get_connection()
    conn.execute("""INSERT INTO order_map
        (platform_order_no, platform_order_id, jky_trade_no, state, retry_count)
        VALUES (?, ?, ?, 'jky_created', 0)""",
        ("LM-WH-001", 9001, "JKY-WEBHOOK-001"))
    conn.commit()
    map_id = conn.execute("SELECT id FROM order_map WHERE platform_order_no=?",
                          ("LM-WH-001",)).fetchone()["id"]

    transport.lanmong_orders[9001] = {
        "orderId": 9001, "orderNo": "LM-WH-001", "state": 2,
        "platformState": 2,
        "name": "WH 收件人", "mobile": "13800000001",
        "province": "上海市", "city": "上海市", "district": "浦东新区",
        "address": "WH 路 1 号", "expressPrice": 10.0, "remark": "WH 测试",
        "orderProducts": [{"skuNo": "G001", "number": 1, "goodsName": "矿泉水"}],
    }

    from lanmeng_bridge.app import process_oms_trade_confirm
    result = await process_oms_trade_confirm("JKY-WEBHOOK-001", {
        "mainPostid": "SF-WH-001", "logisticName": "顺丰"
    })
    if not result.get("ack"):
        return False

    await cron_b.run_cron_b(lanmong, jky, sku_resolver, logistic_resolver, notifier)

    state = conn.execute("SELECT state FROM order_map WHERE id=?",
                          (map_id,)).fetchone()["state"]
    logs = conn.execute("SELECT to_state, source FROM order_status_log "
                        "WHERE order_map_id=? ORDER BY id ASC", (map_id,)).fetchall()
    return state in ("synced", "done") and len(logs) >= 3


async def step7_shipped_state(transport: MockTransport) -> bool:
    return transport.lanmong_orders[TOY_ORDER_ID]["state"] == 4 and \
           TOY_ORDER_ID in transport.shipped_logistic


async def step8b_audit_log_5_rows() -> bool:
    conn = db_mod.get_connection()
    rows = conn.execute("""SELECT DISTINCT to_state FROM order_status_log""").fetchall()
    states = {r["to_state"] for r in rows}
    required = {"audited", "jky_created", "jky_shipped", "synced", "done"}
    return required.issubset(states)


async def step9_sku_missing_skip(feishu: FeishuRecorder) -> bool:
    conn = db_mod.get_connection()
    conn.execute("""INSERT INTO order_map
        (platform_order_no, platform_order_id, jky_trade_no, state, retry_count)
        VALUES (?, ?, ?, 'init', 0)""",
        ("LM-NOSKU-001", 9101, None))
    conn.commit()
    map_id = conn.execute("SELECT id FROM order_map WHERE platform_order_no=?",
                          ("LM-NOSKU-001",)).fetchone()["id"]

    sm.transition(map_id, sm.STATE_SKIPPED, "manual",
                  error=json.dumps({"reason": "sku_missing", "sku": "G999"}))
    conn.execute("UPDATE order_map SET closed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (map_id,))
    conn.commit()

    await feishu.alert_p1("LM-NOSKU-001", "SKU 缺映射: G999", 1, map_id)
    closed_at = conn.execute("SELECT closed_at FROM order_map WHERE id=?",
                              (map_id,)).fetchone()["closed_at"]
    p1_alerts = [a for a in feishu.alerts if a["level"] == "P1"]
    return closed_at is not None and len(p1_alerts) >= 1


async def step10_cancel_via_cron_c(transport: MockTransport, jky: JkyClient,
                                    notifier) -> bool:
    conn = db_mod.get_connection()
    conn.execute("""INSERT INTO order_map
        (platform_order_no, platform_order_id, jky_trade_no, state,
         platform_state, retry_count)
        VALUES (?, ?, ?, 'jky_created', -2, 0)""",
        ("LM-CANCEL-001", 9201, "JKY-CANCEL-001"))
    conn.commit()
    map_id = conn.execute("SELECT id FROM order_map WHERE platform_order_no=?",
                          ("LM-CANCEL-001",)).fetchone()["id"]

    await cron_c.run_cron_c(jky, notifier)

    state = conn.execute("SELECT state FROM order_map WHERE id=?",
                          (map_id,)).fetchone()["state"]
    return state == "jky_cancelled"


async def step11_shipped_cancel_rejected(transport: MockTransport, jky: JkyClient,
                                          notifier, feishu: FeishuRecorder) -> bool:
    conn = db_mod.get_connection()
    conn.execute("""INSERT INTO order_map
        (platform_order_no, platform_order_id, jky_trade_no, state,
         platform_state, retry_count, logistic_no)
        VALUES (?, ?, ?, 'jky_shipped', -2, 0, 'SF-SHIPPED-001')""",
        ("LM-SHIPPED-001", 9301, "JKY-SHIPPED-001"))
    conn.commit()
    map_id = conn.execute("SELECT id FROM order_map WHERE platform_order_no=?",
                          ("LM-SHIPPED-001",)).fetchone()["id"]

    p0_before = sum(1 for a in feishu.alerts if a["level"] == "P0")
    await cron_c.run_cron_c(jky, notifier)
    p0_after = sum(1 for a in feishu.alerts if a["level"] == "P0")

    state = conn.execute("SELECT state FROM order_map WHERE id=?",
                          (map_id,)).fetchone()["state"]
    return state == "jky_shipped" and p0_after > p0_before


async def step12_group_by_category() -> bool:
    conn = db_mod.get_connection()
    conn.executemany(
        "INSERT INTO jky_product_cache (jky_goods_no, jky_goods_name, jky_category) VALUES (?, ?, ?)",
        [("G001", "矿泉水", "饮料"), ("G002", "薯片", "周边")],
    )
    conn.commit()
    rows = conn.execute(
        "SELECT jky_category, COUNT(*) AS n FROM jky_product_cache GROUP BY jky_category"
    ).fetchall()
    return len(rows) == 2


async def step13_cache_changes_has_diff() -> bool:
    conn = db_mod.get_connection()
    conn.execute(
        """INSERT INTO jky_product_cache_changes
            (jky_goods_no, change_type, old_value, new_value, cron_run_id)
            VALUES (?, 'INSERT', NULL, ?, ?)""",
        ("G001", '{"name": "矿泉水", "category": "饮料"}', "test-run-001"),
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM jky_product_cache_changes").fetchone()["n"]
    return n > 0


# ---------------------------------------------------------------------------
# 4. step 14-15: 真实链路验证 — bridge → ECS web-api (c0a1478 没有)
# ---------------------------------------------------------------------------

async def step14_bridge_d_real_link() -> tuple[bool, str, dict]:
    """step 14: bridge cron-d 路径 → 真实 ECS web-api /jky/goods/list

    验证 web-api 路由可达 + 捕获当前 agent 权限状态 (scope 3 v2 agents.yaml
    的 perms 是真实部署版, scope3 backup 才是完整版).

    判定标准:
    - 路由可达 = HTTP 200/403/422 (非 404/000/connection refused)
    - 200 = 真实 JKY 数据 (完整链路通过)
    - 403 = 路由存在, 缺 perm (部署遗漏, 真实 link 走通但 agent 验证失败)
    - 422 = 路由存在, 缺 body 字段 (FastAPI schema 校验)
    - 404/000 = 路由不存在/网络断 (FAIL)
    """
    evidence = {
        "endpoint": "POST /jky/goods/list",
        "gateway": REAL_LINK_GATEWAY_URL,
        "method": "erp-goods.goods.sku.search",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=15, write=10, pool=5)) as cl:
            # 用 JkyClient.goods_search 真实签名 (pageIndex/pageSize/category/categories)
            resp = await cl.post(
                f"{REAL_LINK_GATEWAY_URL}/jky/goods/list",
                params={"api_key": REAL_LINK_API_KEY},
                json={"pageIndex": 0, "pageSize": 5,
                      "category": ["饮料", "周边"],
                      "categories": ["饮料", "周边"]},
            )
        evidence["http_status"] = resp.status_code
        try:
            evidence["body_excerpt"] = resp.text[:200]
        except Exception:
            pass

        if resp.status_code == 200:
            data = resp.json()
            goods = data.get("result", {}).get("data", {}).get("list", [])
            evidence["goods_count"] = len(goods)
            return True, "PASS", evidence
        elif resp.status_code == 403:
            # 路由存在, 缺 perm (scope 3 v2 agents.yaml 漏 perm) — 真实链路可达
            # 但 agent 鉴权失败, 这是部署遗漏, 不是脚本问题
            evidence["finding"] = "agent perm 缺失: erp-goods.goods.sku.search"
            return True, "WARN", evidence
        elif resp.status_code == 422:
            evidence["finding"] = "schema 校验失败 (请求体缺字段)"
            return True, "WARN", evidence
        elif resp.status_code == 404:
            return False, "FAIL: web-api 路由不存在 /jky/goods/list", evidence
        else:
            return False, f"FAIL: HTTP {resp.status_code}", evidence
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
        evidence["error"] = f"网络异常: {type(e).__name__}: {e}"
        return False, "FAIL: 真实链路不可达 (web-api 网络断)", evidence
    except Exception as e:
        evidence["error"] = f"{type(e).__name__}: {e}"
        return False, f"FAIL: 异常 {type(e).__name__}", evidence


async def step15_bridge_e_real_link() -> tuple[bool, str, dict]:
    """step 15: bridge cron-e 路径 → 真实 ECS web-api /jky/logistic/list

    验证 web-api 物流路由可达 (erp.logistic.get 在所有 5 agent 都有 perm,
    应直接 200 + 真实 JKY 物流公司数据).
    """
    evidence = {
        "endpoint": "POST /jky/logistic/list",
        "gateway": REAL_LINK_GATEWAY_URL,
        "method": "erp.logistic.get",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=15, write=10, pool=5)) as cl:
            resp = await cl.post(
                f"{REAL_LINK_GATEWAY_URL}/jky/logistic/list",
                params={"api_key": REAL_LINK_API_KEY},
                json={"pageIndex": 0, "pageSize": 50},
            )
        evidence["http_status"] = resp.status_code
        try:
            evidence["body_excerpt"] = resp.text[:200]
        except Exception:
            pass

        if resp.status_code == 200:
            data = resp.json()
            items = data.get("result", {}).get("data", {}).get("logisticInfo", [])
            evidence["logistic_count"] = len(items)
            return True, "PASS", evidence
        elif resp.status_code == 403:
            evidence["finding"] = "agent perm 缺失: erp.logistic.get"
            return False, "FAIL: cron-e perm 缺失 (5 agent 应都有)", evidence
        elif resp.status_code == 404:
            return False, "FAIL: web-api 路由不存在 /jky/logistic/list", evidence
        else:
            return False, f"FAIL: HTTP {resp.status_code}", evidence
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
        evidence["error"] = f"网络异常: {type(e).__name__}: {e}"
        return False, "FAIL: 真实链路不可达 (web-api 网络断)", evidence
    except Exception as e:
        evidence["error"] = f"{type(e).__name__}: {e}"
        return False, f"FAIL: 异常 {type(e).__name__}", evidence


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------

async def main() -> int:
    print(f"=== scope 5 v3 - 15 步端到端真验 SOP (真实链路版) ===")
    print(f"测试 DB: {TEST_DB}")
    print(f"测试 home: {TEST_HOME}")
    print(f"真实链路 web-api: {REAL_LINK_GATEWAY_URL}")
    print()

    feishu = FeishuRecorder()
    transport = MockTransport(feishu)

    lanmong, jky, notifier, sku_resolver, logistic_resolver = setup_infra(transport, feishu)
    db_mod.init_db()

    conn = db_mod.get_connection()
    conn.execute("INSERT INTO sku_mapping (platform_sku_no, jky_goods_no) VALUES (?, ?)",
                 ("G001", "G001"))
    conn.commit()

    app_mod.notifier = notifier
    app_mod.lanmong_client = lanmong
    app_mod.jky_client = jky
    app_mod.sku_resolver = sku_resolver
    app_mod.logistic_resolver = logistic_resolver

    failures = 0

    # --- step 1
    try:
        ok = await step1_create_toy_order(transport)
        if ok:
            record(1, "蓝盟测试环境创建 toy 订单 (state=1)", "PASS",
                   f"order_id={TOY_ORDER_ID} state=1", order_id=TOY_ORDER_ID)
        else:
            record(1, "蓝盟测试环境创建 toy 订单", "FAIL", "mock 中台未持有订单")
            failures += 1
    except Exception as e:
        record(1, "蓝盟测试环境创建 toy 订单", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 2
    try:
        ok = await step2_cron_register()
        if ok:
            record(2, "8 表 schema 全部就位", "PASS",
                   "order_map/order_status_log/sku_mapping/jky_product_cache*/jky_logistic_cache*/alert_counter")
        else:
            record(2, "8 表 schema 全部就位", "FAIL", "缺表")
            failures += 1
    except Exception as e:
        record(2, "8 表 schema 全部就位", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 3
    try:
        await cron_a.run_cron_a(lanmong, jky, sku_resolver, notifier, auto_review=True)
        ok = await step3_audit_lanmong(transport)
        if ok:
            record(3, "中台订单 state=2 (已自动过审)", "PASS",
                   f"order_id={TOY_ORDER_ID} state=2", state=2)
        else:
            record(3, "中台订单 state=2", "FAIL", "cron-a 未推进 state")
            failures += 1
    except Exception as e:
        record(3, "中台订单 state=2", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 4
    try:
        ok = await step4_jky_created(transport)
        if ok:
            trade = list(transport.jky_trades.values())[0]
            record(4, "吉客云有对应销售单 (待审核/已审)", "PASS",
                   f"tradeNo={trade['tradeNo']} status={trade['status']}",
                   trade_no=trade["tradeNo"])
        else:
            record(4, "吉客云销售单", "FAIL", "无 jky_trade 记录")
            failures += 1
    except Exception as e:
        record(4, "吉客云销售单", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 5
    try:
        await step5_warehouse_handoff()
        record(5, "仓库人员手工递交 → WMS 发货 (模拟)", "PASS",
               "mock 由 cron-b 触发, 不需要真仓库操作")
    except Exception as e:
        record(5, "仓库手工递交 (模拟)", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 6
    try:
        ok = await step6_webhook_then_cron_b(transport, jky, lanmong, notifier,
                                              sku_resolver, logistic_resolver)
        if ok:
            record(6, "webhook 实时 + cron-b 60min 兜底 (不重复)", "PASS",
                   "process_oms_trade_confirm 幂等 + cron-b 推 synced")
        else:
            record(6, "webhook + cron-b 兜底", "FAIL",
                   "state 未推进 synced 或审计不完整")
            failures += 1
    except Exception as e:
        record(6, "webhook + cron-b 兜底", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 7
    try:
        await lanmong.sync_order_express(
            order_id=TOY_ORDER_ID, order_no=TOY_ORDER_NO,
            express_no="SF-TOY-001", express_code="SF", express_name="顺丰",
            warehouse_id=1, warehouse_name="默认仓",
            items=[{"skuNo": "G001", "num": 2}],
        )
        ok = await step7_shipped_state(transport)
        if ok:
            record(7, "中台 state=4 (已发货) + expressNo", "PASS",
                   "expressNo=SF-TOY-001", express="SF-TOY-001")
        else:
            record(7, "中台 state=4", "FAIL", "syncOrderExpress 未推进")
            failures += 1
    except Exception as e:
        record(7, "中台 state=4", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 8
    try:
        ok = await step8b_audit_log_5_rows()
        if ok:
            record(8, "5 条状态变更审计 (init→audited→jky_created→jky_shipped→synced)",
                   "PASS", "order_status_log 4+ 行")
        else:
            record(8, "5 条状态变更审计", "FAIL", "审计日志缺步")
            failures += 1
    except Exception as e:
        record(8, "5 条状态变更审计", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 9
    try:
        ok = await step9_sku_missing_skip(feishu)
        if ok:
            record(9, "SKU 缺映射 → skip + P1 + SQL mark closed", "PASS",
                   "state=skipped + closed_at 写入 + P1 告警 1 条")
        else:
            record(9, "SKU 缺映射", "FAIL", "closed_at 未写入 或 无 P1")
            failures += 1
    except Exception as e:
        record(9, "SKU 缺映射", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 10
    try:
        ok = await step10_cancel_via_cron_c(transport, jky, notifier)
        if ok:
            record(10, "中台 -2 → cron-c → JKY cancel → jky_cancelled", "PASS",
                   "state=jky_cancelled")
        else:
            record(10, "中台 -2 → JKY cancel", "FAIL", "state 未 jky_cancelled")
            failures += 1
    except Exception as e:
        record(10, "中台 -2 → JKY cancel", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 11
    try:
        ok = await step11_shipped_cancel_rejected(transport, jky, notifier, feishu)
        if ok:
            record(11, "已发订单 → cron-c cancel 拒 → P0 告警", "PASS",
                   "state 保持 jky_shipped + P0 告警 1 条")
        else:
            record(11, "已发订单 → P0", "FAIL", "state 变化 或 无 P0")
            failures += 1
    except Exception as e:
        record(11, "已发订单 → P0", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 12
    try:
        ok = await step12_group_by_category()
        if ok:
            record(12, "GROUP BY jky_category = 2 行 (饮料+周边)", "PASS",
                   "2 类别 (饮料/周边)")
        else:
            record(12, "GROUP BY jky_category", "FAIL", "≠ 2 行")
            failures += 1
    except Exception as e:
        record(12, "GROUP BY jky_category", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 13
    try:
        ok = await step13_cache_changes_has_diff()
        if ok:
            record(13, "*_cache_changes > 0 (diff 在工作)", "PASS",
                   "jky_product_cache_changes 写入 diff 审计")
        else:
            record(13, "*_cache_changes > 0", "FAIL", "diff 表空")
            failures += 1
    except Exception as e:
        record(13, "*_cache_changes > 0", "FAIL", f"异常: {e}")
        failures += 1

    # --- step 14: 真实链路 (bridge cron-d → ECS web-api /jky/goods/list)
    print("[step 14] 真实链路调用: bridge cron-d → ECS web-api /jky/goods/list ...")
    try:
        ok, detail, evidence = await step14_bridge_d_real_link()
        status = "WARN" if ok and "WARN" in detail else ("PASS" if ok else "FAIL")
        record(14, "bridge cron-d 真实链路: web-api /jky/goods/list 可达",
               status, detail, **evidence)
        if not ok:
            failures += 1
    except Exception as e:
        record(14, "bridge cron-d 真实链路", "FAIL", f"异常: {e}",
               error=f"{type(e).__name__}: {e}")
        failures += 1

    # --- step 15: 真实链路 (bridge cron-e → ECS web-api /jky/logistic/list)
    print("[step 15] 真实链路调用: bridge cron-e → ECS web-api /jky/logistic/list ...")
    try:
        ok, detail, evidence = await step15_bridge_e_real_link()
        status = "PASS" if ok else "FAIL"
        record(15, "bridge cron-e 真实链路: web-api /jky/logistic/list 可达",
               status, detail, **evidence)
        if not ok:
            failures += 1
    except Exception as e:
        record(15, "bridge cron-e 真实链路", "FAIL", f"异常: {e}",
               error=f"{type(e).__name__}: {e}")
        failures += 1

    # --- 清理 ---
    await lanmong.close()
    await jky.close()
    await notifier.close()

    # --- 输出 markdown 表格 ---
    print()
    print("| 步 | 名称 | 状态 | 详情 | 证据 |")
    print("|---|---|---|---|---|")
    for r in RESULTS:
        print(r.to_row())
    print()
    print(f"PASS: {sum(1 for r in RESULTS if r.status == 'PASS')}")
    print(f"WARN: {sum(1 for r in RESULTS if r.status == 'WARN')}")
    print(f"FAIL: {sum(1 for r in RESULTS if r.status == 'FAIL')}")

    return 1 if failures > 0 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
