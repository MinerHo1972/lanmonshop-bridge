"""cron-b：吉客云 → 中台发货回传（60min 兜底）

全量拉 JKY 订单（15天窗口）→ 对比 DB 刷新 jky_unified + bridge_unified
→ 有物流的调 syncOrderExpress 回传蓝盟 → 更新 platform_unified
"""
import json
import logging
from datetime import datetime, timedelta

from ..clients.lanmonshop import LanmongClient
from ..clients.jky import JkyClient
from ..core.state_machine import transition as st_transition, is_terminal, STATE_JKY_SHIPPED, STATE_SYNCED, \
    STATE_DONE, STATE_FAILED
from ..core.logistic_resolver import LogisticResolver
from ..core.exception_handler import RetryState, classify_error, Severity
from ..core.shared_unified import platform_to_unified, bridge_to_unified, resolve_jky_effective_state, \
    pull_jky_trades_multi_window
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)

# JKY 全量查询参数
LOOKBACK_DAYS = 15
JKY_LOOKBACK_DAYS = 14          # 多窗口分段拉取，每段 ≤7 天（JKY API 硬限制）
JKY_DEFAULT_FIELDS = (
    "tradeNo,onlineTradeNo,tradeStatus,tradeStatusExplain,"
    "mainPostid,logisticName,shopName,scrollId"
)


async def run_cron_b(
    lanmong: LanmongClient,
    jky: JkyClient,
    logistic_resolver: LogisticResolver,
    notifier: FeishuNotifier,
):
    """拉 JKY 全量 → 刷新统一态 → 回传物流"""
    logger.info("[cron-b] 开始 JKY 全量拉取")
    conn = get_connection()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

    # ---- Step 1: 拉 JKY 全量（多窗口分段拉取，每段 ≤7 天）----
    all_jky = {}  # {tradeNo or onlineTradeNo: trade_data}
    try:
        # pull_jky_trades_multi_window 内部按 7 天窗口分段，自动合并去重
        all_jky = await pull_jky_trades_multi_window(jky, JKY_LOOKBACK_DAYS)
    except Exception as e:
        logger.error(f"[cron-b] JKY 全量拉取失败: {e}")
        if notifier:
            await notifier.alert_p1("cron-b", f"JKY 全量拉取全部窗口失败: {e}", 0, 0)

    if not all_jky:
        logger.info("[cron-b] JKY 无订单数据")
        return
    logger.info(f"[cron-b] JKY {len(all_jky)} 条（含双索引）")

    # ---- Step 1.5: 构建拆合单后继索引 ----
    # JKY 合并/拆分后: 原单 tradeStatus=5020, 后继单 onlineTradeNo 含所有原单号(逗号分隔)
    # 这里用 onlineTradeNo 切分构建 platform_order_no → successor_trade 映射
    successor_index = {}  # {platform_order_no: [successor_trade, ...]}
    trade_no_index = {}
    for key, trade in all_jky.items():
        trade_no = str(trade.get("tradeNo", "") or "")
        if trade_no:
            trade_no_index[trade_no] = trade
        ts = str(trade.get("tradeStatus", ""))
        if ts == "5020":
            continue  # 原单跳过, 只有非取消态才可能是后继单
        online = trade.get("onlineTradeNo", "")
        parts = [p.strip() for p in online.split(",") if p.strip()]
        if len(parts) > 1:
            for part in parts:
                successor_index.setdefault(part, []).append(trade)
    if successor_index:
        logger.info(f"[cron-b] 构建 {len(successor_index)} 条拆合单后继索引")

    # ---- Step 2: 查 DB 中匹配的订单 ----
    params = [cutoff]
    successor_order_nos = sorted(successor_index)
    successor_clause = ""
    if successor_order_nos:
        placeholders = ",".join("?" for _ in successor_order_nos)
        successor_clause = f" OR platform_order_no IN ({placeholders})"
        params.extend(successor_order_nos)
    db_rows = conn.execute(
        "SELECT id, platform_order_no, jky_trade_no, jky_state, state, logistic_no, "
        "platform_order_id, platform_unified, jky_unified, "
        "jky_effective_unified, bridge_unified, related_order_nos, order_items_json "
        "FROM order_map WHERE updated_at >= ? "
        f"AND (jky_trade_no IS NOT NULL{successor_clause}) "
        "ORDER BY id ASC",
        params,
    ).fetchall()

    def merge_related_order_nos(existing: str, discovered: list[str]) -> str:
        values = {p.strip() for p in (existing or "").split(",") if p.strip()}
        values.update(p.strip() for p in discovered if p and p.strip())
        return ",".join(sorted(values))

    updated_count = 0
    for row_obj in db_rows:
        row = dict(row_obj)
        trade = None
        if row["jky_trade_no"]:
            trade = trade_no_index.get(str(row["jky_trade_no"]))
        trade = trade or all_jky.get(row["platform_order_no"])
        if not trade and row["platform_order_no"] in successor_index:
            trade = {
                "tradeNo": row["jky_trade_no"] or "",
                "onlineTradeNo": row["platform_order_no"],
                "tradeStatus": row["jky_state"] or "5020",
            }
        if not trade:
            continue

        map_id = row["id"]
        resolved = resolve_jky_effective_state(trade, row["platform_order_no"], all_jky, successor_index)
        raw_ts = resolved["raw_ts"]
        effective_trade = resolved["effective_trade"]
        postid = effective_trade.get("mainPostid", "")
        logist_name = effective_trade.get("logisticName", "")
        raw_jky_unified = resolved["raw_unified"]
        effective_jky_unified = resolved["effective_unified"]
        # bridge 跟随 JKY（有后继单则用后继单状态，无则跟 JKY 原始态）
        # 只有订单还未创建到 JKY 时才走 bridge 状态映射
        new_br_unified = effective_jky_unified if resolved["effective_ts"] else bridge_to_unified(row["state"])
        new_related_order_nos = merge_related_order_nos(
            row.get("related_order_nos") or "",
            resolved["related_order_nos"],
        )

        # 更新原始 JKY 态、拆合后有效态、bridge 态
        old_jky_unified = row["jky_unified"]
        old_effective_unified = row["jky_effective_unified"] or ""
        if (
            old_jky_unified != raw_jky_unified
            or old_effective_unified != effective_jky_unified
            or row.get("bridge_unified") != new_br_unified
            or (new_related_order_nos and row.get("related_order_nos", "") != new_related_order_nos)
        ):
            conn.execute(
                "UPDATE order_map SET jky_state = ?, jky_unified = ?, "
                "jky_effective_unified = ?, bridge_unified = ?, related_order_nos = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (raw_ts, raw_jky_unified, effective_jky_unified, new_br_unified, new_related_order_nos, map_id),
            )
            conn.commit()
            updated_count += 1
            logger.info(f"[cron-b] {row['platform_order_no']} jky: {old_jky_unified}→{raw_jky_unified} "
                        f"effective: {old_effective_unified}→{effective_jky_unified} "
                        f"bridge: {row.get('bridge_unified','?')}→{new_br_unified}")

        # ---- Step 3: 仅当 JKY 订单确认为"已发货"+"已完成"时才回传物流 ----
        # JKY 在 "待发货" 阶段（4112）也可能有预约物流单号（mainPostid），
        # 但不等同于已发货。只能用 tradeStatus 判断，不能只看 postid。
        JKY_SHIPPED_UNIFIED = {"已发货", "已完成"}
        # 终态（done/jky_cancelled/cancelled）不再回传——cron-c 已取消的订单跳过（2026-08-09 修复）
        if effective_jky_unified in JKY_SHIPPED_UNIFIED and postid \
                and row["state"] not in (STATE_DONE, STATE_SYNCED) \
                and not is_terminal(row["state"]):
            # JKY 已发货 + 有物流单号 + 未闭环 + 非终态 → 回传蓝盟
            # H2: 副作用前重读 DB 当前状态，防 cron-b/cron-c 并发竞态（快照可能已过期）
            current_row = conn.execute(
                "SELECT state FROM order_map WHERE id = ?", (map_id,)
            ).fetchone()
            if not current_row or is_terminal(current_row["state"]) \
                    or current_row["state"] == STATE_SYNCED:
                logger.info(f"[cron-b] {row['platform_order_no']} 状态已变终态/已闭环，跳过回传")
                continue
            current_state = current_row["state"]
            # M1: 状态白名单——jky_shipped 可直接回传；audited/jky_created/failed 先转移成功后回传；
            # init/skipped/未知状态跳过（避免非法状态下调用外部接口）
            if current_state not in ("jky_shipped", "audited", "jky_created", "failed"):
                logger.warning(f"[cron-b] {row['platform_order_no']} 状态 {current_state} 不应回传，跳过")
                continue
            if current_state in ("audited", "jky_created", "failed"):
                # M2: 检查转移返回值——转移失败则不得继续调用外部接口
                if not st_transition(map_id, STATE_JKY_SHIPPED, "cron_b"):
                    logger.warning(f"[cron-b] {row['platform_order_no']} 状态转移 {current_state}→jky_shipped 失败，跳过回传")
                    continue

            logistic_entry = logistic_resolver.resolve(logist_name)
            platform_code = logistic_entry.get("platform_code", "")
            platform_name = logistic_entry.get("platform_name", "")
            if not platform_code or platform_code == "unknown":
                logger.warning(f"[cron-b] {row['platform_order_no']} 无法解析物流公司 '{logist_name}'，跳过回传")
                st_transition(map_id, STATE_FAILED, "cron_b", f"未知物流: {logist_name}")
                continue
            express_code = platform_code
            express_name = platform_name or logist_name

            # 解析 items
            items = []
            try:
                order_products = json.loads(row.get("order_items_json") or "[]")
                for prod in order_products:
                    oiid = prod.get("orderItemId")
                    if not oiid:
                        continue
                    num = prod.get("num") or prod.get("number") or 1
                    item = {"orderItemId": int(oiid), "num": int(num)}
                    # 蓝盟 syncOrderExpress 要求 skuId 或 skuNo 至少传其一
                    sku_no = prod.get("skuNo")
                    sku_id = prod.get("skuId")
                    if sku_no:
                        item["skuNo"] = str(sku_no)
                    elif sku_id:
                        item["skuId"] = int(sku_id)
                    items.append(item)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

            # 回退：order_items_json 无有效明细时，按单号实时回查蓝盟补明细
            # （根因：建单快照天生缺 orderProducts，重试无解；回查是唯一可靠数据源）
            if not items:
                fallback_products = []
                try:
                    fb_resp = await lanmong.get_deliver_orders(
                        order_no=row["platform_order_no"],
                        page_size=5,
                        state=None,
                    )
                    if fb_resp.get("code") == 0:
                        fb_data = fb_resp.get("data", {})
                        fb_list = (
                            fb_data.get("orderList", [])
                            if isinstance(fb_data, dict)
                            else (fb_data if isinstance(fb_data, list) else [])
                        )
                        if fb_list:
                            fallback_products = fb_list[0].get("orderProducts") or []
                except Exception as e:
                    logger.warning(
                        f"[cron-b] {row['platform_order_no']} 明细回退回查异常: {e}"
                    )
                if fallback_products:
                    # 写回快照，同单二次回传不再回查
                    try:
                        conn.execute(
                            "UPDATE order_map SET order_items_json = ?, "
                            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (json.dumps(fallback_products, ensure_ascii=False, default=str), map_id),
                        )
                        conn.commit()
                        logger.info(
                            f"[cron-b] {row['platform_order_no']} 明细回退：回查蓝盟补得 "
                            f"{len(fallback_products)} 条明细并写回快照"
                        )
                    except Exception as e:
                        logger.warning(f"[cron-b] {row['platform_order_no']} 明细写回失败: {e}")
                    for prod in fallback_products:
                        oiid = prod.get("orderItemId")
                        if not oiid:
                            continue
                        num = prod.get("num") or prod.get("number") or 1
                        item = {"orderItemId": int(oiid), "num": int(num)}
                        sku_no = prod.get("skuNo")
                        sku_id = prod.get("skuId")
                        if sku_no:
                            item["skuNo"] = str(sku_no)
                        elif sku_id:
                            item["skuId"] = int(sku_id)
                        items.append(item)
                else:
                    logger.warning(
                        f"[cron-b] {row['platform_order_no']} 快照无明细且回查未补得，需人工处理"
                    )
            if not items:
                logger.warning(f"[cron-b] {row['platform_order_no']} 无有效商品明细，跳过回传")
                st_transition(map_id, STATE_FAILED, "cron_b", "无商品明细")
                continue

            retry = RetryState()
            success = False
            while not retry.is_exhausted and not success:
                try:
                    sync_resp = await lanmong.sync_order_express(
                        order_id=row.get("platform_order_id", 0),
                        order_no=row["platform_order_no"],
                        express_no=postid,
                        express_code=express_code,
                        express_name=express_name,
                        warehouse_id=0, warehouse_name="虚拟仓",
                        items=items,
                    )
                    if sync_resp.get("code") == 0:
                        # 检查 faultList：蓝盟返回 code=0 时 faultList 可能仍有失败项
                        fault_list = (sync_resp.get("data") or {}).get("faultList") or []
                        if fault_list:
                            error = "; ".join(
                                f.get("errorMsg", "")
                                for f in fault_list
                                if f.get("errorMsg")
                            ) or "回传局部失败"
                            retry.attempt = retry.max_attempts  # 数据问题，重试无意义
                            retry.last_error = error
                            logger.warning(
                                f"[cron-b] {row['platform_order_no']} 蓝盟回传 faultList: "
                                f"{[f.get('errorMsg','') for f in fault_list]}"
                            )
                            continue
                        # 只记物流单号 + 推进 bridge 状态机, 不动 platform_unified
                        conn.execute(
                            "UPDATE order_map SET logistic_no = ?, "
                            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (postid, map_id),
                        )
                        conn.commit()
                        st_transition(map_id, STATE_SYNCED, "cron_b")
                        st_transition(map_id, STATE_DONE, "cron_b")
                        logger.info(f"[cron-b] {row['platform_order_no']} 回传成功 → done")
                        # 回传成功后，拉蓝盟该单实时 state 更新 platform_state
                        try:
                            lanm_resp = await lanmong.get_deliver_orders(
                                order_no=row["platform_order_no"],
                                page_size=5,
                                state=None,
                            )
                            lanm_data = lanm_resp.get("data", {})
                            order_list = (
                                lanm_data.get("orderList", [])
                                if isinstance(lanm_data, dict)
                                else (lanm_data if isinstance(lanm_data, list) else [])
                            )
                            if order_list:
                                actual_state = order_list[0].get("state")
                                if actual_state is not None and str(actual_state) != str(row.get("platform_state", "")):
                                    new_unified = platform_to_unified(actual_state)
                                    conn.execute(
                                        "UPDATE order_map SET platform_state = ?, platform_unified = ?, "
                                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                        (actual_state, new_unified, map_id),
                                    )
                                    conn.commit()
                                    logger.info(
                                        f"[cron-b] {row['platform_order_no']} "
                                        f"platform_state {row.get('platform_state','?')} → {actual_state}"
                                    )
                        except Exception as e:
                            logger.warning(
                                f"[cron-b] {row['platform_order_no']} 拉蓝盟实际态失败（非致命）: {e}"
                            )
                        success = True
                    else:
                        error = sync_resp.get("msg", "syncOrderExpress 失败")
                        retry.record_attempt(error)
                        logger.warning(f"[cron-b] {row['platform_order_no']} 回传失败 (retry={retry.attempt}): {error}")
                except Exception as e:
                    retry.record_attempt(str(e))
                    logger.warning(f"[cron-b] {row['platform_order_no']} 回传异常 (retry={retry.attempt}): {e}")

            if not success:
                conn.execute(
                    "UPDATE order_map SET retry_count = ?, last_error = ? WHERE id = ?",
                    (retry.attempt, retry.last_error, map_id),
                )
                conn.commit()
                # H1 修正版：回传失败后查蓝盟实际态——若蓝盟已取消，则这是预期行为
                # （订单已取消，物流回传必然失败），不升 P1；但绝不写 jky_cancelled 终态，
                # JKY 取消仍由 cron-c 正规执行。查不到可靠证据则维持原 P1（fail-closed）。
                lanm_cancelled = False
                try:
                    lanm_resp = await lanmong.get_deliver_orders(
                        order_no=row["platform_order_no"],
                        page_size=5,
                        state=None,
                    )
                    if lanm_resp.get("code") == 0:
                        lanm_data = lanm_resp.get("data", {})
                        order_list = (
                            lanm_data.get("orderList", [])
                            if isinstance(lanm_data, dict)
                            else (lanm_data if isinstance(lanm_data, list) else [])
                        )
                        # M1 fail-closed: 精确匹配 orderNo + 严格映射为已取消/退款才抑制
                        for o in order_list:
                            if str(o.get("orderNo", "")) == str(row["platform_order_no"]):
                                actual_state = o.get("state")
                                if actual_state is not None and platform_to_unified(actual_state) == "已取消/退款":
                                    lanm_cancelled = True
                                    new_unified = platform_to_unified(actual_state)
                                    conn.execute(
                                        "UPDATE order_map SET platform_state = ?, platform_unified = ?, "
                                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                        (actual_state, new_unified, map_id),
                                    )
                                    conn.commit()
                                    logger.warning(
                                        f"[cron-b] {row['platform_order_no']} 蓝盟已取消(state={actual_state})，"
                                        f"物流回传失败属预期，不升 P1；JKY 取消交由 cron-c 处理"
                                    )
                                break
                except Exception as e:
                    # M1: 查询失败不能抑制 P1（fail-closed）
                    logger.warning(f"[cron-b] {row['platform_order_no']} 蓝盟实际态复核失败（维持 P1）: {e}")
                if not lanm_cancelled:
                    # M2 补充：发 P1 前重读 DB——若已被 cron-c 转终态（jky_cancelled/cancelled），
                    # 视为可靠证据抑制 P1（避免机械告警），订单已由 cron-c 处理
                    final_row = conn.execute(
                        "SELECT state FROM order_map WHERE id = ?", (map_id,)
                    ).fetchone()
                    if final_row and is_terminal(final_row["state"]):
                        logger.warning(
                            f"[cron-b] {row['platform_order_no']} 发 P1 前重读为终态 {final_row['state']}，"
                            f"抑制 P1（cron-c 已处理）"
                        )
                    else:
                        st_transition(map_id, STATE_FAILED, "cron_b", retry.last_error)
                        await notifier.alert_p1(
                            "cron-b", f"订单 {row['platform_order_no']} 回传失败: {retry.last_error or '回传失败'}",
                            retry.attempt, map_id,
                        )

    logger.info(f"[cron-b] 完成 (刷新 {updated_count} 条)")
