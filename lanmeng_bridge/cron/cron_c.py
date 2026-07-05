"""cron-c：三端取消/退款对比兜底（15min）

拉蓝盟 + JKY 两端全量（15天窗口）
→ 对比 DB 统一态
→ 蓝盟取消但 JKY 未取消 → 调 JKY cancel + 写统一字段
→ JKY 取消但蓝盟未取消 → 飞书告警（避免资损）
"""
import logging
from datetime import datetime, timedelta

from ..clients.jky import JkyClient
from ..clients.lanmonshop import LanmongClient
from ..core.state_machine import transition as st_transition, STATE_JKY_CANCELLED
from ..core.shared_unified import platform_to_unified, resolve_jky_effective_state, \
    pull_jky_trades_multi_window
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)
LOOKBACK_DAYS = 15
JKY_LOOKBACK_DAYS = 14
JKY_SHOP_IDS = "2154377951944409856"  # 特渠分销对接 店铺 ID


async def run_cron_c(
    jky: JkyClient,
    notifier: FeishuNotifier,
    lanmong: LanmongClient = None,
):
    """三端取消对比兜底"""
    logger.info("[cron-c] 开始三端取消对比")
    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

    # ---- Step 1: 拉蓝盟已取消订单 ----
    lanmong_cancelled = {}  # {orderNo: state}
    if lanmong:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            resp = await lanmong.get_deliver_orders(
                state="-2,-3,-4",
                supplier_update_time_start=cutoff,
                supplier_update_time_end=now_str,
                page_size=200,
            )
            if resp.get("code") != 0:
                logger.warning(f"[cron-c] 蓝盟查询异常: code={resp.get('code')} msg={resp.get('msg','')}")
            else:
                resp_data = resp.get("data", {})
                orders = (resp_data.get("orderList", [])
                          if isinstance(resp_data, dict)
                          else (resp_data if isinstance(resp_data, list) else []))
                for o in orders:
                    no = o.get("orderNo", "")
                    st = o.get("state")
                    if no and st is not None:
                        lanmong_cancelled[no] = st
                logger.info(f"[cron-c] 蓝盟已取消 {len(lanmong_cancelled)} 条")
        except Exception as e:
            logger.warning(f"[cron-c] 拉蓝盟取消失败: {e}")

    # ---- Step 2: 拉 JKY 全量用于取消检测+拆合单识别 ----
    all_jky = {}   # {tradeNo or onlineTradeNo: trade_data}
    jky_cancelled_ts = {}  # {tradeNo or onlineTradeNo: tradeStatus} 仅真正取消（非拆合单）
    try:
        all_jky = await pull_jky_trades_multi_window(jky, JKY_LOOKBACK_DAYS)

        successor_index = {}
        for t_data in all_jky.values():
            ts = str(t_data.get("tradeStatus", "") or "")
            if ts == "5020" or ts == "5030":
                continue
            online_parts = [p.strip() for p in t_data.get("onlineTradeNo", "").split(",") if p.strip()]
            if len(online_parts) > 1:
                for part in online_parts:
                    successor_index.setdefault(part, []).append(t_data)

        # 从全量中筛选出确实取消的单，同时检测拆合单后继
        # 5010/4122 → 真正取消，直接计入
        # 5020/5030 → 合并/拆分原单，检查是否有后继单还在推进
        cancelled_ts_filter = {"5010", "4122"}
        merge_split_ts = {"5020", "5030"}
        for key, t_data in all_jky.items():
            ts = str(t_data.get("tradeStatus", ""))
            if ts in cancelled_ts_filter:
                jky_cancelled_ts[key] = ts
            elif ts in merge_split_ts:
                # 检查是否有活跃后继单
                platform_no = str(t_data.get("onlineTradeNo", "") or key)
                resolved = resolve_jky_effective_state(t_data, platform_no, all_jky, successor_index)
                if resolved.get("successor_trade_nos"):
                    logger.debug(f"[cron-c] {key} 是{ts}原单, 后继单活跃, 跳过取消检测")
                    continue
                # 无后继单或后继单也已取消 → 视为真正取消
                jky_cancelled_ts[key] = ts
                logger.info(f"[cron-c] {key} 是{ts}原单且无活跃后继, 视为取消")

        logger.info(f"[cron-c] JKY 全量 {len(all_jky)} 条, 其中已取消 {len(jky_cancelled_ts)} 条")
    except Exception as e:
        logger.warning(f"[cron-c] 拉 JKY 全量失败: {e}")

    # ---- Step 3: 查 DB 匹配订单 ----
    db_rows = conn.execute(
        "SELECT id, platform_order_no, jky_trade_no, state, platform_unified, jky_unified, "
        "jky_effective_unified, bridge_unified "
        "FROM order_map WHERE updated_at >= ? ORDER BY id ASC", (cutoff,)
    ).fetchall()

    # 建立索引
    order_no_to_row = {r["platform_order_no"]: dict(r) for r in db_rows}
    trade_no_to_row = {r["jky_trade_no"]: dict(r) for r in db_rows if r["jky_trade_no"]}

    # 建立 all_jky 的 tradeNo 索引（用于从 jky_trade_no 反查 JKY 数据）
    trade_no_to_jky = {}
    for t in all_jky.values():
        tn = str(t.get("tradeNo", "") or "")
        if tn:
            trade_no_to_jky[tn] = t

    # ---- Step 4: 蓝盟取消 → JKY 未取消 → 调 JKY cancel ----
    for order_no, lm_state in lanmong_cancelled.items():
        row = order_no_to_row.get(order_no)
        if not row or not row["jky_trade_no"]:
            continue
        # 蓝盟已取消但 bridge 未取消
        if row["state"] in (STATE_JKY_CANCELLED, "cancelled"):
            continue

        # 拆合单保护：如果该单的后继单已发货，跳过取消
        # 场景：蓝盟原单被取消（-2），但 JKY 已合并到后继单且后继单已发货
        # cron-c 不应因为原单取消就去取消后继单
        jky_trade = trade_no_to_jky.get(row["jky_trade_no"])
        if jky_trade:
            resolved = resolve_jky_effective_state(
                jky_trade, order_no, all_jky, successor_index
            )
            if resolved.get("successor_trade_nos") and resolved.get("effective_unified") in (
                "已发货", "已完成"
            ):
                logger.info(
                    f"[cron-c] {order_no} 是拆合单原单，后继单已发货，跳过取消"
                )
                continue

        jky_trade_no = row["jky_trade_no"]
        logger.info(f"[cron-c] {order_no}({jky_trade_no}) 蓝盟已取消，调 JKY cancel")
        try:
            cancel_resp = await jky.trade_cancel({"tradeNos": jky_trade_no, "cancelReason": "420001"})
            if cancel_resp.get("code") == 200:
                new_jky_unified = "已取消/退款"
                new_br_unified = new_jky_unified  # bridge 跟随 JKY
                new_platform_unified = platform_to_unified(lm_state)
                conn.execute(
                    "UPDATE order_map SET jky_unified = ?, jky_effective_unified = ?, "
                    "bridge_unified = ?, platform_unified = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_jky_unified, new_jky_unified, new_br_unified, new_platform_unified, row["id"]),
                )
                conn.commit()
                st_transition(row["id"], STATE_JKY_CANCELLED, "cron_c")
                logger.info(f"[cron-c] {order_no} JKY 取消成功")
            else:
                logger.warning(f"[cron-c] {order_no} JKY 取消失败: {cancel_resp}")
        except Exception as e:
            logger.warning(f"[cron-c] {order_no} JKY 取消异常: {e}")

    # ---- Step 5: JKY 取消 → 蓝盟正常 → 飞书告警（P0 资损风险）----
    for key, jky_ts in jky_cancelled_ts.items():
        # 先查 onlineTradeNo，再查 tradeNo
        row = order_no_to_row.get(key) or trade_no_to_row.get(key)
        if not row:
            continue
        try:
            lm_unified = row.get("platform_unified") or ""
        except Exception:
            lm_unified = ""
        if lm_unified in ("已取消/退款",):
            continue  # 蓝盟也已取消 → 正常

        # JKY 已取消但蓝盟正常 → 告警
        logger.warning(
            f"[cron-c] P0: {row['platform_order_no']} JKY(jky_ts={jky_ts}) "
            f"已取消但蓝盟(lm_unified={lm_unified})正常"
        )
        await notifier.alert_p0(
            row["platform_order_no"],
            f"JKY tradeStatus={jky_ts} 已取消, 但蓝盟平台态={lm_unified} 正常; "
            f"请确认订单是否需要人工处理",
            row["id"], row["state"],
        )

    logger.info("[cron-c] 完成")
