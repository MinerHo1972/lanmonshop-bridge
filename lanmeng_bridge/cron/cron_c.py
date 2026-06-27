"""cron-c：中台已退 → 吉客云取消（5min 对账）

检测：order_map 中 jky_created 状态 + platform_state=-2/-3/-4
→ 调 JKY /jky/trade/cancel
→ state → jky_cancelled

🆕 P0 边界 (PRD §9.1 + §4.8 已发订单): 若订单已被 webhook 推进到 jky_shipped
(logistic_no IS NOT NULL) → 跳过 cancel，直接 P0 飞书告警 (资损风险)
"""

import logging

from ..clients.jky import JkyClient
from ..core.state_machine import transition, STATE_JKY_CANCELLED, STATE_FAILED
from ..core.exception_handler import RetryState
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)

# 中台异常/取消/退款 state
CANCEL_STATES = {-2, -3, -4}

# 🆕 P0 判定: 吉客云已发货的本地权威信号 (webhook 写入 logistic_no + state 推进)
# PRD §9.1 P0 第 1 条: "吉客云已发货但中台已退 (cron-c 失败 + 货已发)"
SHIPPED_STATES = ("jky_shipped", "synced", "done")


async def run_cron_c(
    jky: JkyClient,
    notifier: FeishuNotifier,
):
    """检测中台已退但吉客云未退 → 调 JKY 取消 (已发订单触发 P0 边界)"""
    logger.info("[cron-c] 开始对账")

    conn = get_connection()
    rows = conn.execute(
        """SELECT id, platform_order_no, jky_trade_no, platform_state,
                  state, logistic_no, retry_count, last_error
        FROM order_map
        WHERE jky_trade_no IS NOT NULL
          AND platform_state IN (-2, -3, -4)
          AND closed_at IS NULL
          AND (
            -- 路径 1: 未发货待取消 (jky_created)
            state = 'jky_created'
            -- 路径 2: P0 边界 — 已发货但中台已退 (logistic_no 非空 + 已发货状态)
            OR (logistic_no IS NOT NULL AND state IN ('jky_shipped', 'synced', 'done'))
          )
        ORDER BY updated_at ASC
        LIMIT 50"""
    ).fetchall()

    if not rows:
        logger.info("[cron-c] 无待取消订单")
        return

    logger.info(f"[cron-c] 发现 {len(rows)} 条待取消订单")
    for row in rows:
        map_id = row["id"]
        order_no = row["platform_order_no"]
        jky_trade_no = row["jky_trade_no"]
        platform_state = row["platform_state"]
        current_state = row["state"]
        logistic_no = row["logistic_no"]

        # 🆕 P0 边界 (PRD §9.1): 已发货订单不调 cancel, 直接 P0 飞书告警
        if logistic_no and current_state in SHIPPED_STATES:
            logger.warning(
                f"[cron-c] P0 边界: {order_no}({jky_trade_no}) 已发货 "
                f"(logistic_no={logistic_no}, state={current_state}) "
                f"但中台已退 (state={platform_state}) → 跳过 cancel, P0 告警"
            )
            await notifier.alert_p0(
                order_no,
                f"中台 state={platform_state} 已退, 但吉客云已发货 "
                f"(logistic_no={logistic_no}, state={current_state}); "
                f"资损风险, 立即人工处理",
                map_id,
                current_state,
            )
            continue  # 不调 cancel, 直接下一条

        # 调 JKY 取消 (仅 jky_created 未发货订单)
        retry = RetryState()
        success = False
        while not retry.is_exhausted and not success:
            try:
                cancel_resp = await jky.trade_cancel({"tradeNo": jky_trade_no})
                if cancel_resp.get("code") == 0:
                    transition(map_id, STATE_JKY_CANCELLED, "cron_c")
                    logger.info(
                        f"[cron-c] {order_no}({jky_trade_no}) 取消成功 "
                        f"(中台 state={platform_state})"
                    )
                    success = True
                else:
                    error = cancel_resp.get("msg", "JKY 取消失败")
                    retry.record_attempt(error)
                    logger.warning(
                        f"[cron-c] {order_no} 取消失败 (retry={retry.attempt}): {error}"
                    )
            except Exception as e:
                retry.record_attempt(str(e))
                logger.warning(
                    f"[cron-c] {order_no} 取消异常 (retry={retry.attempt}): {e}"
                )

        if not success:
            conn.execute(
                """UPDATE order_map SET retry_count = ?, last_error = ?
                WHERE id = ?""",
                (retry.attempt, retry.last_error, map_id),
            )
            conn.commit()
            transition(map_id, STATE_FAILED, "cron_c", retry.last_error)
            await notifier.alert_p1(
                order_no,
                retry.last_error or f"中台 state={platform_state} 取消失败",
                retry.attempt,
                map_id,
            )

    logger.info("[cron-c] 完成")
