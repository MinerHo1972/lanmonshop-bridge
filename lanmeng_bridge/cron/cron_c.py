"""cron-c：中台已退 → 吉客云取消（5min 对账）

检测：order_map 中 jky_created 状态 + platform_state=-2/-3/-4
→ 调 JKY /jky/trade/cancel
→ state → jky_cancelled
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


async def run_cron_c(
    jky: JkyClient,
    notifier: FeishuNotifier,
):
    """检测中台已退但吉客云未退 → 调 JKY 取消"""
    logger.info("[cron-c] 开始对账")

    conn = get_connection()
    rows = conn.execute(
        """SELECT id, platform_order_no, jky_trade_no, platform_state,
                  retry_count, last_error
        FROM order_map
        WHERE state = 'jky_created'
          AND jky_trade_no IS NOT NULL
          AND platform_state IN (-2, -3, -4)
          AND closed_at IS NULL
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

        # 调 JKY 取消
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
