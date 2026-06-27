"""蓝萌API对接项目 — FastAPI 入口 + APScheduler 启动

独立部署在 ECS :18433，走独立的 Cloudflare Tunnel 暴露。
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from .clients.jky import create_jky_client, JkyClient
from .clients.lanmonshop import create_lanmong_client, LanmongClient
from .config import load_settings
from .core.logistic_resolver import LogisticResolver
from .core.sku_resolver import SkuResolver
from .cron import cron_a, cron_b, cron_c, cron_d, cron_e, cron_f
from .notify.feishu import FeishuNotifier
from .storage.db import init_db, close_all

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
