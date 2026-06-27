"""蓝萌API对接项目 — FastAPI 入口 + APScheduler 启动

独立部署在 ECS :18433，走独立的 Cloudflare Tunnel 暴露。
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, HTTPException

from .clients.jky import create_jky_client, JkyClient
from .clients.lanmonshop import create_lanmong_client, LanmongClient
from .config import load_settings
from .core.logistic_resolver import LogisticResolver
from .core.sku_resolver import SkuResolver
from .core.state_machine import transition, STATE_JKY_SHIPPED, STATE_SYNCED, STATE_DONE
from .cron import cron_a, cron_b, cron_c, cron_d, cron_e, cron_f
from .notify.feishu import FeishuNotifier
from .storage.db import init_db, close_all, get_connection

# ---------- 日志 ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lanmonshop-bridge")

# ---------- 全局状态 ----------

settings = load_settings()
scheduler = AsyncIOScheduler()
lanmong_client: LanmongClient = None
jky_client: JkyClient = None
sku_resolver: SkuResolver = None
logistic_resolver: LogisticResolver = None
notifier: FeishuNotifier = None


# ---------- Cron 任务 ----------

async def _run_cron_a():
    global lanmong_client, jky_client, sku_resolver, notifier
    try:
        await cron_a.run_cron_a(
            lanmong_client, jky_client, sku_resolver, notifier,
            auto_review=settings.get("auto_review", True),
        )
    except Exception as e:
        logger.exception(f"[cron-a] 未捕获异常: {e}")


async def _run_cron_b():
    global lanmong_client, jky_client, sku_resolver, logistic_resolver, notifier
    try:
        await cron_b.run_cron_b(
            lanmong_client, jky_client, sku_resolver, logistic_resolver, notifier,
        )
    except Exception as e:
        logger.exception(f"[cron-b] 未捕获异常: {e}")


async def _run_cron_c():
    global jky_client, notifier
    try:
        await cron_c.run_cron_c(jky_client, notifier)
    except Exception as e:
        logger.exception(f"[cron-c] 未捕获异常: {e}")


async def _run_cron_d():
    global jky_client, notifier
    try:
        await cron_d.run_cron_d(jky_client, notifier)
    except Exception as e:
        logger.exception(f"[cron-d] 未捕获异常: {e}")


async def _run_cron_e():
    global jky_client, notifier
    try:
        await cron_e.run_cron_e(jky_client, notifier)
    except Exception as e:
        logger.exception(f"[cron-e] 未捕获异常: {e}")


async def _run_cron_f():
    global lanmong_client, jky_client, notifier
    try:
        await cron_f.run_cron_f(lanmong_client, jky_client, notifier)
    except Exception as e:
        logger.exception(f"[cron-f] 未捕获异常: {e}")


# ---------- 生命周期 ----------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动时初始化，关闭时清理"""
    global lanmong_client, jky_client, sku_resolver, logistic_resolver, notifier

    # 初始化
    logger.info("初始化数据库...")
    init_db(settings.get("db", {}).get("path"))

    logger.info("初始化客户端...")
    lanmong_client = create_lanmong_client(settings)
    jky_client = create_jky_client(settings)
    sku_resolver = SkuResolver()
    logistic_resolver = LogisticResolver()

    # 飞书告警
    feishu_cfg = settings.get("feishu", {})
    creds = settings.get("_credentials", {})
    feishu_webhook = creds.get("feishu", {}).get("webhook_url", "")
    notifier = FeishuNotifier(
        webhook_url=feishu_webhook,
        p0_at_all=feishu_cfg.get("p0_at_all", True),
    )
    if not feishu_webhook:
        logger.warning("飞书 webhook 未配置，告警功能不可用")

    # 注册 cron 任务
    # 并发约束 (PRD §2 P1 可行性修正): APScheduler max_instances=1 + SQLite WAL
    # 防止 cron-a/b/c/d/e/f 同 order 写锁碰撞 + 同订单被并发处理
    cron_cfg = settings.get("cron", {})
    a_interval = cron_cfg.get("a_interval_minutes", 5)
    b_interval = cron_cfg.get("b_interval_minutes", 60)
    c_interval = cron_cfg.get("c_interval_minutes", 5)
    d_hour = cron_cfg.get("d_hour", 2)
    d_minute = cron_cfg.get("d_minute", 0)
    e_hour = cron_cfg.get("e_hour", 2)
    e_minute = cron_cfg.get("e_minute", 30)
    f_hour = cron_cfg.get("f_hour", 3)
    f_minute = cron_cfg.get("f_minute", 30)

    # 通用调度参数: max_instances=1 防止同 cron 重叠, misfire_grace_time + coalesce
    # 防 ECS 短暂不可用后 job 堆叠
    common_kwargs = dict(
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        _run_cron_a, "interval", minutes=a_interval,
        id="cron_a", replace_existing=True,
        **common_kwargs,
    )
    scheduler.add_job(
        _run_cron_b, "interval", minutes=b_interval,
        id="cron_b", replace_existing=True,
        **common_kwargs,
    )
    scheduler.add_job(
        _run_cron_c, "interval", minutes=c_interval,
        id="cron_c", replace_existing=True,
        **common_kwargs,
    )
    scheduler.add_job(
        _run_cron_d, "cron", hour=d_hour, minute=d_minute,
        id="cron_d", replace_existing=True,
        **common_kwargs,
    )
    scheduler.add_job(
        _run_cron_e, "cron", hour=e_hour, minute=e_minute,
        id="cron_e", replace_existing=True,
        **common_kwargs,
    )
    scheduler.add_job(
        _run_cron_f, "cron", hour=f_hour, minute=f_minute,
        id="cron_f", replace_existing=True,
        **common_kwargs,
    )

    scheduler.start()
    logger.info(
        f"Cron 任务已注册 (max_instances=1): "
        f"cron-a({a_interval}min), "
        f"cron-b({b_interval}min), "
        f"cron-c({c_interval}min), "
        f"cron-d(每天 {d_hour:02d}:{d_minute:02d}), "
        f"cron-e(每天 {e_hour:02d}:{e_minute:02d}), "
        f"cron-f(每天 {f_hour:02d}:{f_minute:02d})"
    )

    # ---------- cron-d/e bootstrap（scope 4 实施后改用 cron_d/cron_e 真实逻辑）----------
    # 启动时若 jky_product_cache / jky_logistic_cache 为空，主动拉 1 次
    # 不再使用 _bootstrap_logistic_cache 简化版（已废弃, 改走 cron_e diff-INSERT）
    try:
        _conn = get_connection()
        _prod_cnt = _conn.execute("SELECT COUNT(*) FROM jky_product_cache").fetchone()[0]
        _log_cnt = _conn.execute("SELECT COUNT(*) FROM jky_logistic_cache").fetchone()[0]
        if _prod_cnt == 0:
            logger.info("[bootstrap] jky_product_cache 为空, 立即拉取货品列表")
            asyncio.create_task(cron_d.run_cron_d(jky_client, notifier))
        if _log_cnt == 0:
            logger.info("[bootstrap] jky_logistic_cache 为空, 立即拉取物流公司列表")
            asyncio.create_task(cron_e.run_cron_e(jky_client, notifier))
    except Exception as e:
        logger.warning(f"[bootstrap] 拉取失败（非致命）: {e}")

    yield  # 应用运行中

    # 清理
    logger.info("关闭服务...")
    scheduler.shutdown(wait=False)
    close_all()
    if lanmong_client:
        await lanmong_client.close()
    if jky_client:
        await jky_client.close()
    if notifier:
        await notifier.close()


# ---------- FastAPI 应用 ----------

app = FastAPI(
    title="蓝萌API对接项目",
    description="蓝盟商城中台 ↔ 吉客云 订单中转桥",
    version="0.3.6",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "lanmonshop-bridge"}


@app.get("/")
async def root():
    return {
        "service": "lanmonshop-bridge",
        "version": "0.3.6",
        "scope": "scope 2 - 脚手架 + 6 cron 占位 + 8 表 schema",
        "cron": {
            "cron_a": "中台 → 吉客云 (5min)",
            "cron_b": "吉客云 → 中台兜底 (60min)",
            "cron_c": "中台退 → 吉客云取消 (5min)",
            "cron_d": "货品列表每日拉取 (02:00)",
            "cron_e": "物流公司每日拉取 (02:30, scope 4 实施)",
            "cron_f": "三方状态对账 (03:30, scope 4 实施)",
        },
    }


# ---------- webhook（scope 3 §4.8）----------


def _jky_webhook_sign(params: dict, app_secret: str) -> str:
    """吉客云 webhook D-C 验签（与 jky_gateway.jky_sign 完全一致）

    sign_str = (app_secret + concat_sorted_kv + app_secret).lower()
    expected = md5(sign_str).hexdigest()   # 小写 hex（注意：非 upper）
    excluded: sign, contextid
    """
    filtered = {
        k: v for k, v in params.items()
        if k not in ("sign", "contextid") and v is not None
    }
    concat = "".join(f"{k}{filtered[k]}" for k in sorted(filtered))
    raw = f"{app_secret}{concat}{app_secret}".lower()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _get_jky_app_secret() -> str:
    """从 credentials 读取吉客云 AppSecret（webhook 验签用）"""
    return settings.get("_credentials", {}).get("jky_gateway", {}).get("app_secret", "")


async def process_oms_trade_confirm(trade_no: str, payload: dict) -> dict:
    """处理吉客云 oms.trade.confirm 回调（发货确认）

    scope 3: 状态机推进 + tradeNo 业务幂等 + 审计记录。
    完整 syncOrderExpress 回传中台由 cron-b (scope 4) 兜底完成。
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT id, platform_order_no, state FROM order_map WHERE jky_trade_no = ?",
        (trade_no,),
    ).fetchone()

    if row is None:
        # 非本桥创建的订单 — ack 但不处理
        logger.info(f"[webhook] {trade_no} 非本桥订单，ack 跳过")
        return {"ack": True, "action": "not_tracked"}

    map_id = row["id"]
    current_state = row["state"]

    # 已 synced/done = 幂等 ack（不重复回传）
    if current_state in (STATE_SYNCED, STATE_DONE):
        logger.info(f"[webhook] {trade_no} 已处理（{current_state}），幂等 ack")
        return {"ack": True, "action": "idempotent_skip"}

    # 更新物流单号（如有）
    express_no = payload.get("mainPostid") or payload.get("expressNo") or ""
    logistic_name = payload.get("logisticName") or payload.get("expressName") or ""
    if express_no:
        conn.execute(
            "UPDATE order_map SET logistic_no = ?, last_attempt_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (express_no, map_id),
        )
        conn.commit()

    # 状态机推进 → jky_shipped（发货回传 syncOrderExpress 由 cron-b 兜底）
    if current_state in ("jky_created", "failed", "audited"):
        transition(map_id, STATE_JKY_SHIPPED, "webhook")
        logger.info(
            f"[webhook] {trade_no} → jky_shipped "
            f"(express={express_no}, {logistic_name}); syncOrderExpress 由 cron-b 兜底"
        )
    else:
        # 其它态记录事件即可
        conn.execute(
            "INSERT INTO order_status_log (order_map_id, from_state, to_state, source, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (map_id, current_state, current_state, "webhook",
             json.dumps({"tradeNo": trade_no, "expressNo": express_no}, ensure_ascii=False)),
        )
        conn.commit()

    return {"ack": True, "action": "processed", "state": "jky_shipped"}


@app.post("/jky/webhook/oms.trade.confirm")
async def jky_webhook_oms_trade_confirm(request: Request):
    """吉客云 webhook — 订单发货确认回调（scope 3 §4.8）

    5 步：① 取 sign ② 排除 sign/contextid ③ D-C 验签 ④ tradeNo 业务幂等 ⑤ process_oms_trade_confirm
    """
    app_secret = _get_jky_app_secret()
    if not app_secret:
        logger.error("[webhook] AppSecret 未配置，拒绝回调")
        raise HTTPException(status_code=503, detail="webhook secret not configured")

    # 解析参数（query + json/form body，兼容吉客云 TOP 回调）
    params: dict = {}
    try:
        params.update(dict(request.query_params))
        body = await request.body()
        if body:
            try:
                params.update(json.loads(body))
            except Exception:
                from urllib.parse import parse_qs
                for k, v in parse_qs(body.decode("utf-8", "replace")).items():
                    params[k] = v[0] if len(v) == 1 else v
    except Exception as e:
        logger.warning(f"[webhook] 解析失败: {e}")
        raise HTTPException(status_code=400, detail="bad request")

    received_sign = str(params.pop("sign", "") or "")
    if not received_sign:
        raise HTTPException(status_code=401, detail="missing sign")

    # §4.8 验签
    expected_sign = _jky_webhook_sign(params, app_secret)
    if not hmac.compare_digest(expected_sign, received_sign):
        logger.warning(
            f"[webhook] 验签失败 (expected={expected_sign[:8]}... got={received_sign[:8]}...)"
        )
        raise HTTPException(status_code=401, detail="sign mismatch")

    # tradeNo 业务幂等 + 处理
    trade_no = str(params.get("tradeNo") or params.get("trade_no") or "")
    if not trade_no:
        return {"code": 0, "msg": "ok (no tradeNo)"}

    try:
        result = await process_oms_trade_confirm(trade_no, params)
    except Exception as e:
        logger.exception(f"[webhook] {trade_no} 处理异常: {e}")
        # 不阻塞吉客云重试 → 200 但标记 error
        return {"code": -1, "msg": str(e)}

    return {"code": 0, "msg": "ok", "data": result}


# ---------- 入口 ----------

def main():
    """CLI 入口"""
    import uvicorn
    host = settings.get("service", {}).get("host", "0.0.0.0")
    port = settings.get("service", {}).get("port", 18433)
    uvicorn.run(
        "lanmeng_bridge.app:app",
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
