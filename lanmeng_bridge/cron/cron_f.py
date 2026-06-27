"""cron-f：三方状态对账（中台 / 吉客云 / DB）（1/day @ 03:30 CST）

Scope 4 实施 (v0.3.6 §11.2.10):
- 1/day @ 03:30 CST（cron-e 错峰 60min, 防 SQLite 写锁）
- SELECT * FROM order_map WHERE state IN ['jky_shipped','synced']
- 拉中台 /open/v1/order/getDeliverOrders + 拉吉客云 /jky/trade/list
- 三方 state 偏差 >5 单 → 飞书 P0 资损告警 + 详情
- 偏差 ≤5 单 → 飞书 P2 日志告警（不阻塞）
- cron 自身失败 → 自动重试 1 次 → 仍失败 → 飞书 P1 告警

中台客户端无按 ID 批量查询接口（仅分页 state=N 拉全部），三方对账只能：
- 中台: 分页拉全部 jky_shipped+synced 状态订单（state=4 已发货?）
- 吉客云: trade_list?tradeNos=... 按 tradeNo 批量查
- DB: SELECT from order_map

简化策略（v0.3.6 一期）:
- 中台暂时跳过（避免拉全量导致 cron-f 超时）
- 重点对账 JKY vs DB（吉客云 tradeStatus vs DB order_map.state）
- 偏差定义：DB 标 jky_shipped 但 JKY tradeStatus 不是 已发货/已完成
        或 DB 标 synced 但 JKY tradeStatus 是 已完成
        或 DB 标 synced 但 logistic_no 缺失
- 中台对账留 v0.3.7 二期补（需中台先加 byId 批量接口）

实施参见 PRD v0.3.6 §11.2.10
"""

import json
import logging
import time
from typing import Optional

from ..clients.jky import JkyClient
from ..clients.lanmonshop import LanmongClient
from ..core.exception_handler import RetryState
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)

# 偏差阈值（PRD §11.2.10 + §9.1）
P0_THRESHOLD = 5  # 偏差 > 5 → P0 资损告警
JKY_BATCH_SIZE = 50  # 批量查 JKY trade_list 限制


# 吉客云 tradeStatus 含义（按 P5 已订阅 erp.logistic.get 经验）
# 已完成 = 9090 / "已完成" / "已发货" — 表示吉客云侧流程已闭环
JKY_SHIPPED_STATUSES = {"9090", "已完成", "已发货", "已签收"}


async def _fetch_jky_trades(
    jky: JkyClient,
    trade_nos: list[str],
) -> dict[str, dict]:
    """按 tradeNo 批量查吉客云订单

    Returns:
        {trade_no: jky_order_dict}
    """
    result: dict[str, dict] = {}
    for i in range(0, len(trade_nos), JKY_BATCH_SIZE):
        batch = trade_nos[i : i + JKY_BATCH_SIZE]
        try:
            resp = await jky.trade_list({
                "tradeNos": batch,  # 批量查（JkyClient 已实现 trade_list）
                "pageSize": len(batch),
            })
        except Exception as e:
            logger.warning(f"[cron-f] JKY trade_list 批量查失败 ({len(batch)} 条): {e}")
            continue
        if resp.get("code") != 0:
            logger.warning(f"[cron-f] JKY trade_list 异常: {resp.get('msg', '')}")
            continue
        data = resp.get("data", {})
        trades = data.get("trades", data.get("list", data.get("rows", [])))
        for t in trades:
            tno = t.get("tradeNo") or t.get("trade_no") or ""
            if tno:
                result[tno] = t
    return result


def _detect_deviation(
    db_orders: list,
    jky_trades: dict[str, dict],
) -> list[dict]:
    """对比 DB 与 JKY 状态, 返回偏差列表

    Returns:
        [{"map_id", "platform_order_no", "jky_trade_no", "db_state",
          "jky_status", "reason"}, ...]
    """
    deviations = []
    for row in db_orders:
        map_id = row["id"]
        order_no = row["platform_order_no"]
        jky_trade_no = row["jky_trade_no"]
        db_state = row["state"]
        logistic_no = row["logistic_no"]

        if not jky_trade_no:
            # 没有 JKY 订单号 = 中台有但吉客云无（异常路径）
            deviations.append({
                "map_id": map_id,
                "platform_order_no": order_no,
                "jky_trade_no": None,
                "db_state": db_state,
                "jky_status": None,
                "reason": "DB 标 jky_shipped/synced 但缺 jky_trade_no",
            })
            continue

        jky_trade = jky_trades.get(jky_trade_no)
        if not jky_trade:
            deviations.append({
                "map_id": map_id,
                "platform_order_no": order_no,
                "jky_trade_no": jky_trade_no,
                "db_state": db_state,
                "jky_status": None,
                "reason": "JKY trade_list 查不到此 tradeNo",
            })
            continue

        jky_status = (
            jky_trade.get("tradeStatusExplain")
            or jky_trade.get("tradeStatus")
            or ""
        )
        jky_postid = jky_trade.get("mainPostid", "")

        # 偏差判定 1: DB 标 jky_shipped 但 JKY 状态不是已发货/已完成
        if db_state == "jky_shipped":
            if str(jky_status) not in JKY_SHIPPED_STATUSES:
                deviations.append({
                    "map_id": map_id,
                    "platform_order_no": order_no,
                    "jky_trade_no": jky_trade_no,
                    "db_state": db_state,
                    "jky_status": jky_status,
                    "reason": f"DB jky_shipped 但 JKY 状态 {jky_status!r}",
                })

        # 偏差判定 2: DB 标 synced 但缺 logistic_no
        if db_state == "synced" and not logistic_no:
            deviations.append({
                "map_id": map_id,
                "platform_order_no": order_no,
                "jky_trade_no": jky_trade_no,
                "db_state": db_state,
                "jky_status": jky_status,
                "reason": "DB synced 但 logistic_no 为空",
            })

        # 偏差判定 3: DB 标 synced 但 JKY postid 为空（不应回传但回传了）
        if db_state == "synced" and not jky_postid:
            deviations.append({
                "map_id": map_id,
                "platform_order_no": order_no,
                "jky_trade_no": jky_trade_no,
                "db_state": db_state,
                "jky_status": jky_status,
                "reason": "DB synced 但 JKY mainPostid 为空",
            })

    return deviations


async def run_cron_f(
    lanmong: LanmongClient,
    jky: JkyClient,
    notifier: FeishuNotifier,
) -> None:
    """三方状态对账 (中台 / 吉客云 / DB)

    PRD §11.2.10 v0.3.6 实施
    """
    run_id = f"cron-f-{int(time.time())}"
    logger.info(f"[cron-f] 开始 run_id={run_id}")

    # 1. SELECT DB 中 jky_shipped + synced 订单
    conn = get_connection()
    db_orders = conn.execute(
        """SELECT id, platform_order_no, jky_trade_no, state, logistic_no
        FROM order_map
        WHERE state IN ('jky_shipped', 'synced')
          AND closed_at IS NULL
        ORDER BY updated_at ASC"""
    ).fetchall()

    if not db_orders:
        logger.info("[cron-f] 无 jky_shipped/synced 订单, 跳过对账")
        return

    logger.info(f"[cron-f] DB 中待对账订单 {len(db_orders)} 条")

    trade_nos = [r["jky_trade_no"] for r in db_orders if r["jky_trade_no"]]

    # 2. 拉吉客云 trade_list 批量查
    retry = RetryState(max_attempts=2, backoff_minutes=[5])
    jky_trades: dict[str, dict] = {}
    last_error: Optional[str] = None

    while not retry.is_exhausted:
        try:
            jky_trades = await _fetch_jky_trades(jky, trade_nos)
            break
        except Exception as e:
            last_error = str(e)
            retry.record_attempt(last_error)
            logger.error(f"[cron-f] JKY trade_list 失败 (attempt={retry.attempt}): {e}")
            if retry.is_exhausted:
                await notifier.alert_p1(
                    "cron-f",
                    f"三方对账拉 JKY 失败（重试耗尽）: {last_error}",
                    retry.attempt,
                    0,
                )
                return

    logger.info(f"[cron-f] JKY 返回 {len(jky_trades)} 条订单")

    # 3. 检测偏差
    deviations = _detect_deviation(db_orders, jky_trades)

    if not deviations:
        logger.info(
            f"[cron-f] run_id={run_id} 对账完成: "
            f"checked={len(db_orders)} deviation=0"
        )
        return

    # 4. 偏差分级告警
    deviation_count = len(deviations)
    if deviation_count > P0_THRESHOLD:
        # P0 资损告警
        sample = deviations[:5]  # 详情前 5 条
        detail = "\n".join(
            f"- id={d['map_id']} order={d['platform_order_no']} "
            f"db={d['db_state']} jky={d['jky_status']!r} "
            f"reason={d['reason']}"
            for d in sample
        )
        await notifier._send(  # 用 _send 直发（alert_p0 模板需要 order_no 等参数, 这里三方对账场景定制）
            f"🚨 [P0 三方对账资损风险] run_id={run_id}\n"
            f"偏差总数: {deviation_count} (阈值 > {P0_THRESHOLD})\n"
            f"对账范围: {len(db_orders)} 条 (jky_shipped + synced)\n"
            f"详情 (前 {len(sample)} 条):\n{detail}\n"
            f"@all 立即查 order_map + jky_trade 决定手动干预"
        )
        logger.error(
            f"[cron-f] run_id={run_id} P0 偏差 {deviation_count} > {P0_THRESHOLD}"
        )
    else:
        # P2 日志告警 (不阻塞, 仅记录)
        sample = deviations[:3]
        detail = "\n".join(
            f"- id={d['map_id']} order={d['platform_order_no']} reason={d['reason']}"
            for d in sample
        )
        await notifier._send(
            f"📋 [P2 三方对账偏差] run_id={run_id}\n"
            f"偏差总数: {deviation_count} (阈值 ≤ {P0_THRESHOLD})\n"
            f"对账范围: {len(db_orders)} 条\n"
            f"详情 (前 {len(sample)} 条):\n{detail}\n"
            f"(不阻塞, 仅日志告警)"
        )
        logger.warning(
            f"[cron-f] run_id={run_id} P2 偏差 {deviation_count} ≤ {P0_THRESHOLD}"
        )

    logger.info(
        f"[cron-f] run_id={run_id} 对账完成: "
        f"checked={len(db_orders)} deviation={deviation_count}"
    )