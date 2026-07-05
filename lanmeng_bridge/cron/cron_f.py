"""cron-f：每日三方对账报告（1/day @ 03:30 CST）

每天拉取三端（蓝盟/JKY/DB）近30天订单数据，逐单对比状态，
生成结构化日报，推送飞书 + 落库备查。

对账策略（2026-07-03 重构）：
1. 拉蓝盟 getDeliverOrders 全量订单（所有 state，30天窗口，分页）
2. 拉 DB order_map 近30天记录
3. 按 tradeNo 批量查 JKY 实时状态
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
    3: "部分发货",
    4: "已发货",
    6: "已完成",
    -2: "已取消",
    -3: "已退款",
    -4: "已作废",
}

# 蓝盟需要拉的 state 列表（全量）
LANMENG_STATES = ["-4", "-3", "-2", "1", "2", "3", "4", "6"]

# JKY 已发货/已完成状态
JKY_SHIPPED_STATUSES = {"9090", "已完成", "已发货", "已签收"}

# JKY 时间范围查询限制：跨度不超过 7 天（否则返回 0040139996）
JKY_LOOKBACK_DAYS = 14
JKY_SHOP_IDS = "2154377951944409856"  # 特渠分销对接 店铺 ID


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
    jky: JkyClient, cutoff: str
) -> dict:
    """拉 JKY 全量（时间范围 scroll 分页）包含拆合单后继

    改用时间段全量拉取（同 cron-b），确保后继单（不同 tradeNo）
    也能被拉到，代替按 tradeNo 批量查（会漏后继单）。

    Returns:
        {tradeNo: jky_order_dict}
    """
    result = {}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scroll_id = ""
    while True:
        try:
            resp = await jky.trade_list({
                "scrollId": scroll_id,
                "pageSize": 200,
                "startModified": cutoff,
                "endModified": now_str,
                "shopIds": JKY_SHOP_IDS,
                "fields": "tradeNo,onlineTradeNo,tradeStatus,tradeStatusExplain,"
                          "mainPostid,logisticName,shopName,scrollId,"
                          "receiverName,mobile,phone,state,city,district,address,"
                          "payment,totalFee,"
                          "goodsDetail.goodsNo,goodsDetail.goodsName,"
                          "goodsDetail.sellCount,goodsDetail.sellPrice,goodsDetail.sellTotal",
            })
        except Exception as e:
            logger.warning(f"[cron-f] JKY 全量拉取失败: {e}")
            break
        if resp.get("code") != 200:
            break
        trades = resp.get("result", {}).get("data", {}).get("trades", [])
        if not trades:
            break
        for t in trades:
            tno = t.get("tradeNo") or ""
            if tno:
                result[tno] = t
        scroll_id = resp.get("result", {}).get("data", {}).get("scrollId", "")
        if not scroll_id or len(trades) < 200:
            break
    logger.info(f"[cron-f] JKY 全量返回 {len(result)} 条")
    return result


def _compare_order_fields(lanmeng_order: dict, jky_trade: dict) -> list[dict]:
    """Compare key fields between Blue Alliance and JKY order data.

    Returns a list of field mismatch dicts: [{field, lanmeng_value, jky_value, detail}]
    """
    mismatches = []

    # Helper: safe string conversion
    def _safe_str(v):
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return str(v)
        return str(v).strip()

    # Helper: safe int conversion
    def _safe_int(v):
        if v is None:
            return 0
        try:
            return int(float(str(v)))
        except (ValueError, TypeError):
            return 0

    # 1. Receiver name
    lm_name = _safe_str(lanmeng_order.get("name"))
    jky_name = _safe_str(jky_trade.get("receiverName"))
    if lm_name and jky_name and lm_name != jky_name:
        mismatches.append({
            "field": "receiver",
            "lanmeng_value": lm_name,
            "jky_value": jky_name,
            "detail": "收货人不一致",
        })

    # 2. Phone / mobile
    lm_mobile = _safe_str(lanmeng_order.get("mobile"))
    jky_mobile = _safe_str(jky_trade.get("mobile") or jky_trade.get("phone"))
    if lm_mobile and jky_mobile and lm_mobile != jky_mobile:
        mismatches.append({
            "field": "phone",
            "lanmeng_value": lm_mobile,
            "jky_value": jky_mobile,
            "detail": "联系电话不一致",
        })

    # 3. Address (combine province+city+district+address)
    lm_addr = " ".join(filter(None, [
        _safe_str(lanmeng_order.get("province")),
        _safe_str(lanmeng_order.get("city")),
        _safe_str(lanmeng_order.get("district")),
        _safe_str(lanmeng_order.get("address")),
    ])).strip()
    jky_addr = " ".join(filter(None, [
        _safe_str(jky_trade.get("state")),
        _safe_str(jky_trade.get("city")),
        _safe_str(jky_trade.get("district")),
        _safe_str(jky_trade.get("address")),
    ])).strip()
    if lm_addr and jky_addr and lm_addr != jky_addr:
        mismatches.append({
            "field": "address",
            "lanmeng_value": lm_addr,
            "jky_value": jky_addr,
            "detail": "收件地址不一致",
        })

    # 4. Order total — 蓝盟 costPrice(供货价)=JKY sellPrice(销售价)
    # 蓝盟 orderProducts[].costPrice（供货价，PDF P47）在 bridge 设计中
    # 直接映射为 JKY tradeOrderDetails[].sellPrice。因此可以用
    # sum(costPrice × num) 作为预期 JKY totalFee/payment 进行对比。
    # 参见：docs/中台对外开放接口规范-蓝盟-20260622.pdf P46-47
    jky_total = 0
    try:
        jky_total = float(jky_trade.get("totalFee") or jky_trade.get("payment") or 0)
    except (ValueError, TypeError):
        jky_total = 0

    lm_products = lanmeng_order.get("orderProducts") or []
    lm_goods_total = 0.0
    for p in lm_products:
        cost = float(p.get("costPrice") or 0)
        qty = _safe_int(p.get("num") or p.get("number"))
        lm_goods_total += round(cost * qty, 2)

    if jky_total <= 0 and lm_goods_total > 0:
        mismatches.append({
            "field": "total_fee",
            "lanmeng_value": f"供货价合计{lm_goods_total:.2f}（→JKY 销售价）",
            "jky_value": f"{jky_total:.2f}",
            "detail": f"JKY 订单金额为 0（蓝盟供货价合计={lm_goods_total:.2f}），创单时金额字段未正确传递",
        })
    elif abs(jky_total - lm_goods_total) > 0.01 and jky_total > 0 and lm_goods_total > 0:
        mismatches.append({
            "field": "total_fee",
            "lanmeng_value": f"供货价合计{lm_goods_total:.2f}（→JKY 销售价）",
            "jky_value": f"{jky_total:.2f}",
            "detail": f"金额不一致（蓝盟供货价合计={lm_goods_total:.2f}, JKY={jky_total:.2f}）",
        })

    # 5. Product comparison — match by goodsNo/productNo (not by index)
    jky_goods = jky_trade.get("goodsDetail") or []
    if lm_products and jky_goods:
        # Build indexes by productNo/goodsNo (YX 前缀转换后以 jky_goods_no 做 key)
        from ..core.sku_resolver import SkuResolver
        _resolver_f = SkuResolver()
        lm_by_no = {}
        for p in lm_products:
            no = _safe_str(p.get("productNo"))
            if no and no.startswith("YX"):
                jky_no = _resolver_f.resolve(no)
                if jky_no:
                    no = jky_no
            if no:
                lm_by_no[no] = p
        jky_by_no = {}
        for g in jky_goods:
            no = _safe_str(g.get("goodsNo"))
            if no:
                jky_by_no[no] = g

        product_details = []

        # Check count
        lm_count = len(lm_products)
        jky_count = len(jky_goods)
        if lm_count != jky_count and not lm_by_no and not jky_by_no:
            # Only flag count difference if no productNo to match (index compare unreliable)
            mismatches.append({
                "field": "product_count",
                "lanmeng_value": str(lm_count),
                "jky_value": str(jky_count),
                "detail": f"商品数量不一致（蓝盟{lm_count}件, JKY{jky_count}件）",
            })

        # If we can match by productNo → detailed per-item comparison
        if lm_by_no and jky_by_no:
            all_nos = set(lm_by_no) | set(jky_by_no)
            for no in sorted(all_nos):
                p = lm_by_no.get(no)
                g = jky_by_no.get(no)
                if not g:
                    product_details.append(
                        f"货品「{_safe_str(p.get('productName'))}」({no}) 在 JKY 中不存在"
                    )
                    continue
                if not p:
                    product_details.append(
                        f"货品「{_safe_str(g.get('goodsName'))}」({no}) 在蓝盟中不存在"
                    )
                    continue
                # Compare name
                p_name = _safe_str(p.get("productName"))
                g_name = _safe_str(g.get("goodsName"))
                if p_name and g_name and p_name != g_name:
                    product_details.append(
                        f"{no}: 蓝盟「{p_name}」vs JKY「{g_name}」"
                    )
                # Compare quantity
                p_qty = _safe_int(p.get("num") or p.get("number"))
                g_qty = _safe_int(g.get("sellCount"))
                if p_qty != g_qty:
                    product_details.append(
                        f"{no}: 数量不一致（蓝盟{p_qty} vs JKY{g_qty}）"
                    )
        else:
            # Fallback: index-based comparison with safe access
            for i, lm_prod in enumerate(lm_products):
                p_name = _safe_str(lm_prod.get("productName"))
                p_qty = _safe_int(lm_prod.get("num") or lm_prod.get("number"))
                if i < len(jky_goods):
                    g_name = _safe_str(jky_goods[i].get("goodsName"))
                    g_qty = _safe_int(jky_goods[i].get("sellCount"))
                    if p_name and g_name and p_name != g_name:
                        product_details.append(
                            f"第{i+1}件: 蓝盟「{p_name}」vs JKY「{g_name}」"
                        )
                    if p_qty != g_qty:
                        product_details.append(
                            f"第{i+1}件数量: 蓝盟{p_qty} vs JKY{g_qty}"
                        )
                else:
                    product_details.append(f"第{i+1}件「{p_name}」JKY 无对应明细")

        if product_details:
            mismatches.append({
                "field": "product_detail",
                "lanmeng_value": f"{lm_count}件",
                "jky_value": f"{jky_count}件",
                "detail": "; ".join(product_details[:8]),
            })

    return mismatches


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

        # ---- 字段级一致性比对（仅当蓝盟+JKY 两端均有数据时）----
        if jky_trade_no and jky_trade_no in jky_trades:
            field_mismatches = _compare_order_fields(order, jky_trades[jky_trade_no])
            if field_mismatches:
                deviations.append({
                    "order_no": order_no,
                    "db_state": db_state,
                    "lanmong_state": lanmong_state,
                    "lanmong_state_label": lanmong_state_label,
                    "jky_trade_no": jky_trade_no,
                    "jky_status": jky_status,
                    "reason": "字段级不一致",
                    "field_mismatches": field_mismatches,
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
            # 字段级差异详情
            fm = d.get("field_mismatches", [])
            if fm:
                for f in fm[:5]:
                    lines.append(f"   ├ {f['detail']}")
                if len(fm) > 5:
                    lines.append(f"   └ ... 共 {len(fm)} 处字段不一致")
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
        """SELECT id, platform_order_no, jky_trade_no, jky_state, state,
                  logistic_no, platform_state, platform_unified,
                  jky_unified, jky_effective_unified, bridge_unified,
                  related_order_nos,
                  last_error, updated_at
           FROM order_map
           WHERE updated_at >= ?
           ORDER BY id ASC""",
        (cutoff,),
    ).fetchall()
    logger.info(f"[cron-f] DB 订单 {len(db_orders)} 条")

    # ---- 3. 拉 JKY 全量（时间范围 scroll，同 cron-b）----
    jky_trades = {}
    retry = RetryState(max_attempts=2, backoff_minutes=[5])
    while not retry.is_exhausted:
        try:
            jky_trades = await pull_jky_trades_multi_window(
                jky, JKY_LOOKBACK_DAYS,
                fields=(
                    "tradeNo,onlineTradeNo,tradeStatus,tradeStatusExplain,"
                    "mainPostid,logisticName,shopName,scrollId,"
                    "receiverName,mobile,phone,state,city,district,address,"
                    "payment,totalFee,"
                    "goodsDetail.goodsNo,goodsDetail.goodsName,"
                    "goodsDetail.sellCount,goodsDetail.sellPrice,goodsDetail.sellTotal"
                ),
            )
            break
        except Exception as e:
            retry.record_attempt(str(e))
            logger.error(f"[cron-f] JKY 全量拉取失败 (attempt={retry.attempt}): {e}")
            if retry.is_exhausted:
                await notifier._send(
                    f"[cron-f] 警告: JKY 全量拉取失败 (重试耗尽): {e}\n"
                    "JKY 侧状态不可用于对账"
                )
                await notifier.alert_p1("cron-f", f"JKY 全量拉取全部窗口失败 (重试耗尽): {e}", 0, 0)
                break

    # ---- 3b. 兜底刷新 DB 统一态字段 ----
    from ..core.shared_unified import platform_to_unified, resolve_jky_effective_state, \
        pull_jky_trades_multi_window
    successor_index = {}
    for jky_data in jky_trades.values():
        ts = str(jky_data.get("tradeStatus", "") or "")
        if ts == "5020":
            continue
        online_parts = [p.strip() for p in jky_data.get("onlineTradeNo", "").split(",") if p.strip()]
        if len(online_parts) > 1:
            for part in online_parts:
                successor_index.setdefault(part, []).append(jky_data)

    def merge_related_order_nos(existing: str, discovered: list[str]) -> str:
        values = {p.strip() for p in (existing or "").split(",") if p.strip()}
        values.update(p.strip() for p in discovered if p and p.strip())
        return ",".join(sorted(values))

    refresh_count = 0
    for r in db_orders:
        order_no = r["platform_order_no"]
        changes = []

        # 蓝盟态刷新
        if order_no in lanmong_orders:
            lm_state = lanmong_orders[order_no].get("state", r["platform_state"])
            new_lm = platform_to_unified(lm_state)
            old_lm = r["platform_unified"] or ""
            if new_lm != old_lm:
                changes.append(f"platform: {old_lm}→{new_lm}")
                conn.execute(
                    "UPDATE order_map SET platform_unified = ? WHERE id = ?",
                    (new_lm, r["id"]),
                )

        # JKY 态刷新（含拆合单检测）
        trade_no = r["jky_trade_no"]
        jky_data = jky_trades.get(trade_no) if trade_no else None
        if not jky_data and order_no in successor_index:
            jky_data = {
                "tradeNo": trade_no or "",
                "onlineTradeNo": order_no,
                "tradeStatus": r["jky_state"] or "5020",
            }
        if jky_data:
            resolved = resolve_jky_effective_state(jky_data, order_no, jky_trades, successor_index)
            raw_ts = resolved["raw_ts"]
            new_jky = resolved["raw_unified"]
            new_effective = resolved["effective_unified"]
            new_br = new_effective  # bridge 跟随 JKY 有效态
            new_related = merge_related_order_nos(r["related_order_nos"] or "", resolved["related_order_nos"])
            old_jky = r["jky_unified"] or ""
            old_effective = r["jky_effective_unified"] or ""
            old_br = r["bridge_unified"] or ""
            if new_jky != old_jky:
                changes.append(f"jky: {old_jky}→{new_jky}")
            if new_effective != old_effective:
                changes.append(f"effective: {old_effective}→{new_effective}")
            if new_br != old_br:
                changes.append(f"bridge: {old_br}→{new_br}")
            if new_related and new_related != (r["related_order_nos"] or ""):
                changes.append(f"related_order_nos: {r['related_order_nos'] or ''}→{new_related}")
            if new_jky != old_jky or new_effective != old_effective or new_br != old_br or (
                new_related and new_related != (r["related_order_nos"] or "")
            ):
                conn.execute(
                    "UPDATE order_map SET jky_state = ?, jky_unified = ?, "
                    "jky_effective_unified = ?, bridge_unified = ?, related_order_nos = ? WHERE id = ?",
                    (raw_ts, new_jky, new_effective, new_br, new_related, r["id"]),
                )

        if changes:
            refresh_count += 1
            logger.info(f"[cron-f] 刷新 {order_no}: {'; '.join(changes)}")
    conn.commit()
    logger.info(f"[cron-f] 统一态刷新 {refresh_count} 条")

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
