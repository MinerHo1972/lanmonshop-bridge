"""cron-f：每日三方对账报告（1/day @ 03:30 CST）

每天拉取三端（蓝盟/JKY/DB）近30天订单数据，逐单对比状态，
生成结构化日报，推送飞书 + 落库备查。

对账策略（2026-07-03 重构）：
1. 拉蓝盟 getDeliverOrders 全量订单（所有 state，30天窗口，分页）
2. 拉 DB order_map 近30天记录
3. 按 tradeNos 批量查 JKY 实时状态
4. 三端逐单匹配 → 偏差检测
5. 汇总 + 日报推送 + 落库
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from ..clients.jky import JkyClient
from ..clients.lanmonshop import LanmongClient
from ..core.exception_handler import RetryState
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)

# 蓝盟 state 含义
LANMENG_STATE_MAP = {
    1: "已支付待审核",
    2: "待发货",
    4: "已发货",
    -2: "已取消",
    -3: "已退款",
    -4: "已作废",
}

# 蓝盟需要拉的 state 列表（全量）
LANMENG_STATES = ["-2", "1", "2", "4"]

# JKY 已发货/已完成状态
JKY_SHIPPED_STATUSES = {"9090", "已完成", "已发货", "已签收"}

JKY_BATCH_SIZE = 50


async def _pull_lanmong_orders(
    lanmong: LanmongClient, cutoff: str
) -> dict:
    """拉取蓝盟近30天所有订单（分页，全量 state）

    Returns:
        {orderNo: order_dict}
    """
    result = {}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for state in LANMENG_STATES:
        page = 1
        while True:
            try:
                resp = await lanmong.get_deliver_orders(
                    state=state,
                    page_num=page,
                    page_size=200,
                    supplier_update_time_start=cutoff,
                    supplier_update_time_end=now_str,
                )
            except Exception as e:
                logger.warning(f"[cron-f] 蓝盟拉取失败 (state={state} page={page}): {e}")
                break

            resp_data = resp.get("data", {})
            if isinstance(resp_data, dict):
                orders = resp_data.get("orderList", [])
                total = resp_data.get("total", 0)
            else:
                orders = resp_data if isinstance(resp_data, list) else []
                total = len(orders)

            if not orders:
                break

            for order in orders:
                order_no = order.get("orderNo", "")
                if order_no:
                    result[order_no] = order

            # 判断是否还有下一页
            if len(orders) < 200 or (total and page * 200 >= total):
                break
            page += 1

    logger.info(f"[cron-f] 蓝盟拉取完成: {len(result)} 单 ({LANMENG_STATES})")
    return result


async def _pull_jky_trades(
    jky: JkyClient, trade_nos: list
) -> dict:
    """按 tradeNo 批量查吉客云订单

    Returns:
        {tradeNo: jky_order_dict}
    """
    result = {}
    for i in range(0, len(trade_nos), JKY_BATCH_SIZE):
        batch = trade_nos[i: i + JKY_BATCH_SIZE]
        try:
            resp = await jky.trade_list({
                "tradeNos": ",".join(batch),
                "pageSize": len(batch),
            })
        except Exception as e:
            logger.warning(f"[cron-f] JKY 批量查失败 ({len(batch)} 条): {e}")
            continue
        if resp.get("code") not in (0, 200):
            continue
        data = resp.get("result", {}).get("data", {})
        trades = data.get("trades", data.get("list", data.get("rows", [])))
        for t in trades:
            tno = t.get("tradeNo") or t.get("trade_no") or ""
            if tno:
                result[tno] = t
    logger.info(f"[cron-f] JKY 返回 {len(result)} 条")
    return result


def _build_report(
    lanmong_orders: dict,
    db_orders: list,
    jky_trades: dict,
) -> dict:
    """三端对账核心：逐单比对 → 汇总 + 趋势 + 偏差"""

    # 构建 DB 索引
    db_by_order_no: dict = {}       # platform_order_no → row
    db_trade_nos: set = set()       # 所有 jky_trade_no
    for row in db_orders:
        db_by_order_no[row["platform_order_no"]] = row
        if row["jky_trade_no"]:
            db_trade_nos.add(row["jky_trade_no"])

    # 蓝盟 → DB 匹配情况
    lanmong_matched = 0
    lanmong_unmatched = 0

    # 每日趋势 (date → counts)
    daily = defaultdict(lambda: {"lanmong": 0, "db": 0, "jky_created": 0, "done": 0})

    deviations = []

    # ---- 遍历蓝盟订单 ----
    for order_no, order in lanmong_orders.items():
        lanmong_state = order.get("state")
        lanmong_state_label = LANMENG_STATE_MAP.get(lanmong_state, str(lanmong_state))

        # 更新蓝盟侧每日计数（用 supplierUpdateTime）
        update_time = order.get("supplierUpdateTime", "")
        day_key = update_time[:10] if update_time and len(update_time) >= 10 else \
            datetime.now().strftime("%Y-%m-%d")
        daily[day_key]["lanmong"] += 1

        db_row = db_by_order_no.get(order_no)
        if not db_row:
            lanmong_unmatched += 1
            # 蓝盟有但 DB 没有 → 新订单或拉取窗口偏差
            deviations.append({
                "order_no": order_no,
                "db_state": None,
                "lanmong_state": lanmong_state,
                "lanmong_state_label": lanmong_state_label,
                "jky_trade_no": None,
                "jky_status": None,
                "reason": f"蓝盟有单 (state={lanmong_state_label}) 但 DB 未追踪",
            })
            continue

        lanmong_matched += 1
        db_state = db_row["state"]
        jky_trade_no = db_row["jky_trade_no"]
        daily[day_key]["db"] += 1

        # ---- 检查 JKY 状态 ----
        jky_status = None
        jky_postid = None
        if jky_trade_no and jky_trade_no in jky_trades:
            daily[day_key]["jky_created"] += 1
            jt = jky_trades[jky_trade_no]
            jky_status = jt.get("tradeStatusExplain") or jt.get("tradeStatus") or ""
            jky_postid = jt.get("mainPostid") or ""

        if db_state == "done":
            daily[day_key]["done"] += 1

        # ---- 偏差判定 ----

        # 1. 蓝盟已取消但 DB 未取消
        if lanmong_state in (-2, -3, -4) and db_state not in (
            "jky_cancelled", "cancelled", "done"
        ):
            deviations.append({
                "order_no": order_no,
                "db_state": db_state,
                "lanmong_state": lanmong_state,
                "lanmong_state_label": lanmong_state_label,
                "jky_trade_no": jky_trade_no,
                "jky_status": jky_status,
                "reason": f"蓝盟已取消 ({lanmong_state_label}) 但 DB 未同步 (state={db_state})",
            })
            continue

        # 2. DB done 但蓝盟未发货
        if db_state == "done" and lanmong_state != 4:
            deviations.append({
                "order_no": order_no,
                "db_state": db_state,
                "lanmong_state": lanmong_state,
                "lanmong_state_label": lanmong_state_label,
                "jky_trade_no": jky_trade_no,
                "jky_status": jky_status,
                "reason": f"DB 已闭环但蓝盟未发货 (蓝盟 state={lanmong_state_label})",
            })
            continue

        # 3. DB 已发货但 JKY 状态异常（只检有 tradeNo 的）
        if jky_trade_no and db_state in ("jky_shipped", "synced"):
            if jky_status and str(jky_status) not in JKY_SHIPPED_STATUSES:
                deviations.append({
                    "order_no": order_no,
                    "db_state": db_state,
                    "lanmong_state": lanmong_state,
                    "lanmong_state_label": lanmong_state_label,
                    "jky_trade_no": jky_trade_no,
                    "jky_status": jky_status,
                    "reason": f"DB={db_state} 但 JKY 状态是 {jky_status!r}",
                })
                continue

        # 4. DB synced 但 JKY 缺物流单号
        if db_state == "synced" and jky_trade_no and not jky_postid:
            deviations.append({
                "order_no": order_no,
                "db_state": db_state,
                "lanmong_state": lanmong_state,
                "lanmong_state_label": lanmong_state_label,
                "jky_trade_no": jky_trade_no,
                "jky_status": jky_status,
                "reason": "DB=synced 但 JKY mainPostid 为空",
            })
            continue

    # ---- 补充：DB 有但蓝盟没返回的订单（可能是窗口外或无更新） ----
    db_order_nos = set(db_by_order_no.keys())
    lanmeng_order_nos = set(lanmong_orders.keys())
    db_only = db_order_nos - lanmeng_order_nos
    for order_no in db_only:
        row = db_by_order_no[order_no]
        if row["state"] in ("done", "cancelled", "jky_cancelled", "skipped"):
            continue  # 终态订单不报
        deviations.append({
            "order_no": order_no,
            "db_state": row["state"],
            "lanmong_state": None,
            "lanmong_state_label": "未返回",
            "jky_trade_no": row["jky_trade_no"],
            "jky_status": None,
            "reason": f"DB 追踪中 ({row['state']}) 但蓝盟未返回（可能是窗口外或无更新）",
        })

    # ---- 统计 ----
    jky_success = sum(1 for r in db_orders if r["jky_trade_no"])
    jky_shipped = sum(1 for r in db_orders if r["state"]
                      in ("jky_shipped", "synced", "done"))
    done = sum(1 for r in db_orders if r["state"] == "done")

    # 转化每日趋势为有序列表
    sorted_dates = sorted(daily.keys())
    trend = [
        {
            "date": d,
            "lanmong": daily[d]["lanmong"],
            "db": daily[d]["db"],
            "jky_created": daily[d]["jky_created"],
            "done": daily[d]["done"],
        }
        for d in sorted_dates
    ]

    summary = {
        "lanmong_total": len(lanmong_orders),
        "lanmong_matched": lanmong_matched,
        "lanmong_unmatched": lanmong_unmatched,
        "db_total": len(db_orders),
        "jky_created": jky_success,
        "jky_shipped": jky_shipped,
        "done": done,
        "deviations": len(deviations),
        "pending": sum(1 for r in db_orders if r["state"]
                       not in ("done", "cancelled", "jky_cancelled", "skipped")),
    }

    return {
        "summary": summary,
        "trend": trend,
        "deviations": deviations,
    }


def _format_feishu_report(report: dict) -> str:
    """格式化日报为飞书消息文本"""
    s = report["summary"]
    lines = [
        "📊 蓝盟-吉客云 三方对账日报",
        f"日期: {datetime.now().strftime('%Y-%m-%d')}",
        "",
        "━━ 概览 ━━━━━━━━━━━━━━━━━━━",
        f"蓝盟近30日订单: {s['lanmong_total']}",
        f"  其中 DB 已追踪: {s['lanmong_matched']}",
        f"  DB 未追踪(新单): {s['lanmong_unmatched']}",
        f"DB 追踪中: {s['db_total']}",
        f"JKY 已创单: {s['jky_created']}",
        f"JKY 已发货/回传: {s['jky_shipped']}",
        f"已闭环(done): {s['done']}",
        f"待处理: {s['pending']}",
        "",
    ]

    # 偏差
    devs = report["deviations"]
    if devs:
        lines.append(f"━━ 三端不一致 ({len(devs)}) ━━━━━━━━━━━━")
        for i, d in enumerate(devs[:10], 1):
            lines.append(f"{i}. {d['order_no']}")
            lines.append(f"   DB={d['db_state']} | "
                         f"蓝盟={d['lanmong_state_label']} | "
                         f"JKY={d['jky_status'] or '-'}")
            lines.append(f"   原因: {d['reason']}")
        if len(devs) > 10:
            lines.append(f"   ... 共 {len(devs)} 条, 详情见管理后台")
        lines.append("")
    else:
        lines.append("✅ 三端一致，无偏差")
        lines.append("")

    # 每日趋势（近7天）
    trend = report["trend"]
    recent = [t for t in trend if t["date"] >= (
        datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")]
    if recent:
        lines.append("━━ 近7日趋势 ━━━━━━━━━━━━━━━")
        lines.append("日期      | 蓝盟 | DB | JKY | 闭环")
        lines.append("----------|------|----|-----|-----")
        for t in recent:
            lines.append(
                f"{t['date'][5:]}   | {t['lanmong']:>3} | "
                f"{t['db']:>2} | {t['jky_created']:>3} | {t['done']:>3}"
            )
        lines.append("")

    lines.append("管理后台: https://bridge.minerho1972.ccwu.cc/admin/（🔄 对账 tab）")
    return "\n".join(lines)


async def run_cron_f(
    lanmong: LanmongClient,
    jky: JkyClient,
    notifier: FeishuNotifier,
) -> None:
    """每日三方对账报告"""
    run_id = f"cron-f-{int(time.time())}"
    logger.info(f"[cron-f] 开始 run_id={run_id}")

    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    # ---- 1. 拉蓝盟全量 ----
    retry = RetryState(max_attempts=2, backoff_minutes=[5])
    lanmong_orders = {}
    while not retry.is_exhausted:
        try:
            lanmong_orders = await _pull_lanmong_orders(lanmong, cutoff)
            break
        except Exception as e:
            retry.record_attempt(str(e))
            logger.error(f"[cron-f] 蓝盟拉取失败 (attempt={retry.attempt}): {e}")
            if retry.is_exhausted:
                await notifier._send(
                    f"[cron-f] 严重: 蓝盟拉取全部失败 (重试耗尽): {e}\n"
                    "当日对账报告不完整"
                )
                return

    # ---- 2. 拉 DB ----
    conn = get_connection()
    db_orders = conn.execute(
        """SELECT id, platform_order_no, jky_trade_no, state,
                  logistic_no, platform_state, last_error, updated_at
           FROM order_map
           WHERE updated_at >= ?
           ORDER BY updated_at ASC""",
        (cutoff,),
    ).fetchall()
    logger.info(f"[cron-f] DB 订单 {len(db_orders)} 条")

    # ---- 3. 拉 JKY ----
    trade_nos = [
        r["jky_trade_no"] for r in db_orders if r["jky_trade_no"]
    ]
    jky_trades = {}
    if trade_nos:
        retry = RetryState(max_attempts=2, backoff_minutes=[5])
        while not retry.is_exhausted:
            try:
                jky_trades = await _pull_jky_trades(jky, trade_nos)
                break
            except Exception as e:
                retry.record_attempt(str(e))
                logger.error(f"[cron-f] JKY 批量查失败 (attempt={retry.attempt}): {e}")
                if retry.is_exhausted:
                    await notifier._send(
                        f"[cron-f] 警告: JKY 批量查失败 (重试耗尽): {e}\n"
                        "JKY 侧状态不可用于对账"
                    )
                    break

    # ---- 4. 三端对账 ----
    report = _build_report(lanmong_orders, db_orders, jky_trades)
    logger.info(f"[cron-f] 对账完成: "
                f"蓝盟={report['summary']['lanmong_total']} "
                f"DB={report['summary']['db_total']} "
                f"偏差={report['summary']['deviations']}")

    # ---- 5. 落库 ----
    conn.execute(
        """INSERT INTO reconciliation_report
           (report_date, run_id, summary_json, deviations_json, daily_trend_json)
           VALUES (?, ?, ?, ?, ?)""",
        (
            datetime.now().strftime("%Y-%m-%d"),
            run_id,
            json.dumps(report["summary"], ensure_ascii=False),
            json.dumps(report["deviations"], ensure_ascii=False)
            if report["deviations"] else "[]",
            json.dumps(report["trend"], ensure_ascii=False),
        ),
    )
    conn.commit()

    # ---- 6. 推飞书 ----
    msg = _format_feishu_report(report)
    await notifier._send(msg)
    logger.info(f"[cron-f] run_id={run_id} 完成")
