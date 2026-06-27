"""cron-b：吉客云 → 中台发货回传（60min 兜底）

兜底轮询：拉吉客云已发货订单（tradeStatus=9090 已完成 / mainPostid 非空）
→ 查 order_map 确认为本桥创建的订单
→ 调中台 syncOrderExpress 回传物流
→ state → synced → done
"""

import logging

from ..clients.lanmonshop import LanmongClient
from ..clients.jky import JkyClient
from ..core.state_machine import transition, STATE_JKY_SHIPPED, STATE_SYNCED, \
    STATE_DONE, STATE_FAILED
from ..core.sku_resolver import SkuResolver
from ..core.logistic_resolver import LogisticResolver
from ..core.exception_handler import RetryState, classify_error, Severity
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)


async def run_cron_b(
    lanmong: LanmongClient,
    jky: JkyClient,
    sku_resolver: SkuResolver,
    logistic_resolver: LogisticResolver,
    notifier: FeishuNotifier,
):
    """吉客云 → 中台 发货回传（兜底）"""
    logger.info("[cron-b] 开始兜底轮询")

    # 查本桥创建的、状态在 jky_created 或 jky_shipped 的订单
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, platform_order_no, platform_order_id, jky_trade_no,
                  state, retry_count, last_error
        FROM order_map
        WHERE state IN ('jky_created', 'jky_shipped', 'failed')
          AND jky_trade_no IS NOT NULL
          AND closed_at IS NULL
        ORDER BY updated_at ASC
        LIMIT 50"""
    ).fetchall()

    if not rows:
        logger.info("[cron-b] 无待处理订单")
        return

    logger.info(f"[cron-b] 处理 {len(rows)} 条订单")
    for row in rows:
        map_id = row["id"]
        order_no = row["platform_order_no"]
        jky_trade_no = row["jky_trade_no"]

        # 查 JKY 订单状态
        try:
            list_resp = await jky.trade_list({
                "tradeNo": jky_trade_no,
                "pageSize": 10,
            })
        except Exception as e:
            logger.error(f"[cron-b] {jky_trade_no} 查询失败: {e}")
            continue

        if list_resp.get("code") != 0:
            logger.warning(f"[cron-b] {jky_trade_no} JKY 查询异常: {list_resp}")
            continue

        trades = list_resp.get("data", {}).get("trades", [])
        if not trades:
            continue

        jky_order = trades[0]
        logist_name = jky_order.get("logisticName", "")
        postid = jky_order.get("mainPostid", "")
        trade_status = jky_order.get("tradeStatus", "")
        status_explain = jky_order.get("tradeStatusExplain", "")

        # 已发货 = mainPostid 非空
        if not postid:
            if status_explain in ("已完成", "9090") and not postid:
                # 已完成但无物流单号？跳过
                logger.warning(f"[cron-b] {jky_trade_no} 已完成但无物流单号")
            continue

        # 更新状态为已发货
        # 如果之前不是 jky_shipped 态，先转移
        current_state = row["state"]
        if current_state in ("jky_created", "failed"):
            transition(map_id, STATE_JKY_SHIPPED, "cron_b")

        # 拼接中台回传参数
        logistic_entry = logistic_resolver.resolve(logist_name)
        express_code = logistic_entry.get("platform_code", "unknown")
        express_name = logistic_entry.get("platform_name", logist_name)

        # 反查商品项（从 order_map 关联的原始订单）
        # 简单方案：通过 jky_trade_no 反查 assemblyGoodsDetail 不太可行
        # 此处简化：回传时只传物流信息，商品列表由中台自行匹配
        items = [{"skuNo": "", "num": 0}]  # 占位，实际需从订单详情获取

        # 回传中台
        retry = RetryState()
        success = False
        while not retry.is_exhausted and not success:
            try:
                sync_resp = await lanmong.sync_order_express(
                    order_id=row["platform_order_id"],
                    order_no=order_no,
                    express_no=postid,
                    express_code=express_code,
                    express_name=express_name,
                    warehouse_id=0,
                    warehouse_name="虚拟仓",
                    items=items,
                )
                if sync_resp.get("result") == 0:
                    # 成功
                    conn.execute(
                        "UPDATE order_map SET logistic_no = ? WHERE id = ?",
                        (postid, map_id),
                    )
                    conn.commit()
                    transition(map_id, STATE_SYNCED, "cron_b")
                    transition(map_id, STATE_DONE, "cron_b")
                    logger.info(f"[cron-b] {order_no} 回传成功 → done")
                    success = True
                else:
                    error = sync_resp.get("msg", "syncOrderExpress 失败")
                    retry.record_attempt(error)
                    if not retry.is_exhausted:
                        # 简单等待后重试（实际由 APScheduler 下次触发）
                        logger.warning(
                            f"[cron-b] {order_no} 回传失败 (retry={retry.attempt}): {error}"
                        )
            except Exception as e:
                retry.record_attempt(str(e))
                logger.warning(
                    f"[cron-b] {order_no} 回传异常 (retry={retry.attempt}): {e}"
                )

        if not success:
            # 重试耗尽 → 飞书 P1 告警
            conn.execute(
                """UPDATE order_map SET retry_count = ?, last_error = ?
                WHERE id = ?""",
                (retry.attempt, retry.last_error, map_id),
            )
            conn.commit()
            transition(map_id, STATE_FAILED, "cron_b", retry.last_error)
            await notifier.alert_p1(
                order_no, retry.last_error or "回传失败", retry.attempt, map_id,
            )

    logger.info("[cron-b] 完成")
