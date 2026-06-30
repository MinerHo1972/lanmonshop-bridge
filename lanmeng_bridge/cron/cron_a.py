"""cron-a：中台 → 吉客云（5min）

流程：
1. 调中台 getDeliverOrders(state=1) 拉待发货订单
2. 逐个过审（reviewOrder）
3. 查 sku_mapping → 组装 assemblyGoodsDetail
4. 调 JKY /jky/trade/create 创建吉客云销售单
5. 调 JKY /jky/trade/audit 审核
6. 记录 state 变更到 order_map + order_status_log
"""

import logging

from ..clients.lanmonshop import LanmongClient
from ..clients.jky import JkyClient
from ..core.state_machine import transition, STATE_INIT, STATE_AUDITED, \
    STATE_JKY_CREATED, STATE_SKIPPED, STATE_CANCELLED
from ..core.sku_resolver import SkuResolver
from ..core.exception_handler import RetryState, classify_error, Severity
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)


async def run_cron_a(
    lanmong: LanmongClient,
    jky: JkyClient,
    sku_resolver: SkuResolver,
    notifier: FeishuNotifier,
    auto_review: bool = True,
):
    """中台 → 吉客云 订单同步"""
    logger.info("[cron-a] 开始拉单")

    try:
        resp = await lanmong.get_deliver_orders(state="1")
    except Exception as e:
        logger.error(f"[cron-a] 拉单失败: {e}")
        return

    resp_data = resp.get("data", {})
    # 文档 P45-47: data 可能为 []（空结果）或 {orderList: [...], total: N}
    if isinstance(resp_data, dict):
        orders = resp_data.get("orderList", [])
    else:
        orders = resp_data if isinstance(resp_data, list) else []
    if not orders:
        logger.info("[cron-a] 无新订单")
        return

    logger.info(f"[cron-a] 拉取到 {len(orders)} 条待处理订单")
    conn = get_connection()

    for order in orders:
        order_no = order.get("orderNo", "")
        order_id = order.get("orderId", 0)
        state = order.get("state", 1)

        # 跳过异常/取消/退款订单
        if state in (-2, -3, -4):
            # 记录 cancelled 不进主流程
            conn.execute(
                """INSERT OR IGNORE INTO order_map
                    (platform_order_no, platform_order_id, platform_state, state)
                VALUES (?, ?, ?, ?)""",
                (order_no, order_id, state, STATE_CANCELLED),
            )
            conn.commit()
            logger.info(f"[cron-a] 跳过异常订单 {order_no} (state={state})")
            continue

        # 检查是否已存在
        existing = conn.execute(
            "SELECT id, state FROM order_map WHERE platform_order_no = ?",
            (order_no,),
        ).fetchone()
        if existing and existing["state"] != STATE_INIT:
            logger.debug(f"[cron-a] {order_no} 已处理 (state={existing['state']})")
            continue

        # 插入 order_map（init 态）
        if not existing:
            conn.execute(
                """INSERT OR IGNORE INTO order_map
                    (platform_order_no, platform_order_id, platform_state, state)
                VALUES (?, ?, ?, ?)""",
                (order_no, order_id, state, STATE_INIT),
            )
            conn.commit()
            existing = conn.execute(
                "SELECT id FROM order_map WHERE platform_order_no = ?",
                (order_no,),
            ).fetchone()

        map_id = existing["id"]

        # 自动过审
        if auto_review:
            try:
                review_resp = await lanmong.review_order(order_no)  # 文档: 传渠道单号, 非数字ID
                if review_resp.get("code") != 0:
                    logger.warning(f"[cron-a] {order_no} 过审失败: {review_resp}")
                    continue
                transition(map_id, STATE_AUDITED, "cron_a")
                logger.info(f"[cron-a] {order_no} 过审成功")
            except Exception as e:
                logger.error(f"[cron-a] {order_no} 过审异常: {e}")
                transition(map_id, STATE_FAILED, "cron_a", str(e))
                continue

        # 解析 SKU
        products = order.get("orderProducts", [])
        assembly_detail = []
        skip_order = False
        for item in products:
            sku_no = item.get("skuNo", "")
            qty = item.get("number", 1)
            jky_goods_no = sku_resolver.resolve(sku_no)
            if not jky_goods_no:
                logger.warning(f"[cron-a] {order_no} SKU {sku_no} 缺映射")
                # 记录缺映射，跳过此订单
                transition(map_id, STATE_SKIPPED, "cron_a",
                           f"SKU {sku_no} 缺映射")
                await notifier.alert_p1(
                    order_no, f"SKU {sku_no} 缺映射", 0, map_id
                )
                skip_order = True
                break
            assembly_detail.append({
                "goodsNo": jky_goods_no,
                "qty": qty,
            })

        if skip_order:
            continue

        # 创建吉客云销售单
        receiver_addr = (
            f"{order.get('province', '')}"
            f"{order.get('city', '')}"
            f"{order.get('district', '')}"
            f"{order.get('address', '')}"
        )
        create_biz = {
            "onlineTradeNo": str(order_id),
            "receiverName": order.get("name", ""),
            "receiverMobile": order.get("mobile", ""),
            "receiverAddress": receiver_addr,
            "expressPrice": order.get("expressPrice", 0),
            "buyerMemo": order.get("remark", ""),
            "assemblyGoodsDetail": assembly_detail,
        }
        try:
            create_resp = await jky.trade_create(create_biz)
            jky_code = create_resp.get("code", -1)
            if jky_code != 0:
                logger.error(f"[cron-a] {order_no} JKY 创单失败: {create_resp}")
                transition(map_id, STATE_FAILED, "cron_a",
                           create_resp.get("msg", "JKY 创单失败"))
                continue
            jky_trade_no = create_resp.get("data", {}).get("result", {}).get("tradeNo", "")
            conn.execute(
                "UPDATE order_map SET jky_trade_no = ? WHERE id = ?",
                (jky_trade_no, map_id),
            )
            conn.commit()
            logger.info(f"[cron-a] {order_no} → JKY {jky_trade_no} 创单成功")
        except Exception as e:
            logger.error(f"[cron-a] {order_no} JKY 创单异常: {e}")
            transition(map_id, STATE_FAILED, "cron_a", str(e))
            continue

        # 审核吉客云销售单
        if jky_trade_no:
            try:
                audit_resp = await jky.trade_audit({"tradeNo": jky_trade_no})
                if audit_resp.get("code") != 0:
                    logger.warning(f"[cron-a] {order_no} JKY 审核失败: {audit_resp}")
                    # 创单成功但审核失败 → 人工处理
                    await notifier.alert_p1(
                        order_no,
                        f"JKY 创单成功但审核失败: {audit_resp.get('msg', '')}",
                        0, map_id,
                    )
                else:
                    logger.info(f"[cron-a] {order_no} JKY {jky_trade_no} 审核成功")
            except Exception as e:
                logger.warning(f"[cron-a] {order_no} JKY 审核异常: {e}")

            transition(map_id, STATE_JKY_CREATED, "cron_a")

    logger.info("[cron-a] 完成")
