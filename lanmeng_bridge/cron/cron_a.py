"""cron-a：中台 → 吉客云（5min）

拉蓝盟 1(待审核),2(待发货) 全量订单 → 对比 DB 刷新 platform_unified
→ 未处理订单过审 → JKY 创单 → 写 jky_unified + bridge_unified
"""
import json
import logging
from datetime import datetime, timedelta

from ..clients.lanmonshop import LanmongClient
from ..clients.jky import JkyClient
from ..core.state_machine import transition as st_transition, STATE_INIT, STATE_AUDITED, \
    STATE_JKY_CREATED, STATE_SKIPPED, STATE_CANCELLED, STATE_FAILED
from ..core.exception_handler import RetryState, classify_error, Severity
from ..core.shared_unified import platform_to_unified, jky_to_unified, bridge_to_unified
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection, get_cursor, set_cursor

logger = logging.getLogger(__name__)
CURSOR_KEY = "cron_a_last_pull"
LOOKBACK_DAYS = 15


async def run_cron_a(
    lanmong: LanmongClient,
    jky: JkyClient,
    notifier: FeishuNotifier,
    auto_review: bool = True,
):
    """拉蓝盟 → 对比 DB → 过审 → 创 JKY 单"""
    logger.info("[cron-a] 开始拉单")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

    # ---- Step 1: 拉蓝盟全量（1,2）----
    all_lanmong = {}  # {orderNo: {state, orderId, orderProducts, ...}}
    page = 1
    while True:
        try:
            resp = await lanmong.get_deliver_orders(
                state="1,2",
                supplier_update_time_start=cutoff,
                supplier_update_time_end=now_str,
                page_num=page,
                page_size=200,
            )
        except Exception as e:
            logger.error(f"[cron-a] 拉单失败 (page={page}): {e}")
            break
        resp_data = resp.get("data", {})
        orders = (resp_data.get("orderList", [])
                  if isinstance(resp_data, dict)
                  else (resp_data if isinstance(resp_data, list) else []))
        if not orders:
            break
        for o in orders:
            no = o.get("orderNo", "")
            if no:
                all_lanmong[no] = o
        total = resp_data.get("total", len(orders)) if isinstance(resp_data, dict) else len(orders)
        total_pages = max(1, (total + 199) // 200)
        if page >= total_pages:
            break
        page += 1

    if not all_lanmong:
        logger.info("[cron-a] 蓝盟无新订单")
        set_cursor(CURSOR_KEY, now_str)
        return

    logger.info(f"[cron-a] 蓝盟 {len(all_lanmong)} 条订单")
    conn = get_connection()

    # ---- Step 2: 对比 DB，刷新 platform_unified ----
    existing_rows = conn.execute(
        "SELECT id, platform_order_no, platform_unified FROM order_map "
        "WHERE updated_at >= ? ORDER BY id ASC", (cutoff,)
    ).fetchall()
    existing_map = {r["platform_order_no"]: dict(r) for r in existing_rows}

    for order_no, order in all_lanmong.items():
        lm_state = order.get("state", 1)
        new_unified = platform_to_unified(lm_state)

        if order_no in existing_map:
            row = existing_map[order_no]
            if row["platform_unified"] != new_unified:
                conn.execute(
                    "UPDATE order_map SET platform_unified = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?", (new_unified, row["id"]),
                )
                logger.info(f"[cron-a] 刷新 {order_no}: {row['platform_unified']} → {new_unified}")
        else:
            # 新订单 — 插入 DB
            conn.execute(
                """INSERT OR IGNORE INTO order_map
                   (platform_order_no, platform_order_id, platform_state, state, platform_unified)
                VALUES (?, ?, ?, ?, ?)""",
                (order_no, order.get("orderId", 0), lm_state, STATE_INIT, new_unified),
            )
            logger.info(f"[cron-a] 新单 {order_no} platform_unified={new_unified}")
    conn.commit()

    # 重新拉一遍 DB 中 init 态的新单（上面 insert 的）
    rows = conn.execute(
        "SELECT id, platform_order_no, platform_order_id, platform_state "
        "FROM order_map WHERE state = ? AND platform_order_no IN ({})".format(
            ",".join("?" for _ in all_lanmong)
        ),
        [STATE_INIT, *all_lanmong.keys()]
    ).fetchall()

    for row in rows:
        order_no = row["platform_order_no"]
        order = all_lanmong.get(order_no)
        if not order:
            continue
        map_id = row["id"]
        order_id = row["platform_order_id"]
        lm_state = order.get("state", 1)

        # 跳过异常订单
        if lm_state in (-2, -3, -4):
            st_transition(map_id, STATE_CANCELLED, "cron_a")
            logger.info(f"[cron-a] {order_no} 异常跳过 (state={lm_state})")
            continue

        # ---- Step 3: 自动过审 ----
        if auto_review:
            try:
                review_resp = await lanmong.review_order(order_no)
                if review_resp.get("code") != 0:
                    logger.warning(f"[cron-a] {order_no} 过审失败: {review_resp}")
                    continue
                st_transition(map_id, STATE_AUDITED, "cron_a")
                logger.info(f"[cron-a] {order_no} 过审成功")
            except Exception as e:
                logger.error(f"[cron-a] {order_no} 过审异常: {e}")
                st_transition(map_id, STATE_FAILED, "cron_a", str(e))
                continue

        # ---- Step 4: 解析 SKU ----
        products = order.get("orderProducts", [])
        trade_order_details = []
        skip_order = False
        for item in products:
            product_no = item.get("productNo", "")
            qty = item.get("number", 1)
            if not product_no:
                logger.warning(f"[cron-a] {order_no} 缺 productNo，跳过")
                st_transition(map_id, STATE_SKIPPED, "cron_a", "缺 productNo")
                skip_order = True
                break
            prod_row = conn.execute(
                "SELECT jky_barcode, jky_goods_name FROM jky_product_cache WHERE jky_goods_no = ?",
                (product_no,),
            ).fetchone()
            if not prod_row:
                logger.warning(f"[cron-a] {order_no} {product_no} 不在缓存")
                st_transition(map_id, STATE_SKIPPED, "cron_a", f"{product_no} 无缓存")
                skip_order = True
                break
            trade_order_details.append({
                "goodsNo": product_no,
                "barcode": prod_row["jky_barcode"] or "",
                "goodsName": prod_row["jky_goods_name"] or "",
                "specName": "默认", "unit": "件",
                "sellPrice": 0, "sellCount": qty, "sellTotal": 0,
            })

        if skip_order:
            continue

        # ---- Step 5: JKY 创单 ----
        receiver_mobile = order.get("mobile", "")
        create_biz = {
            "tradeOrder": {
                "onlineTradeNo": order_no,
                "shopName": "特渠分销对接", "shopCode": "0125", "warehouseCode": "02",
                "tradeTime": now_str, "tradeType": 1,
                "totalFee": 0, "payment": 0, "chargeCurrency": "人民币",
                "receiverName": order.get("name", ""),
                "mobile": receiver_mobile, "phone": receiver_mobile,
                "state": order.get("province", ""), "city": order.get("city", ""),
                "district": order.get("district", ""), "address": order.get("address", ""),
                "logisticCode": "STO", "logisticName": "申通快递", "logisticType": 1,
                "payStatus": 9, "chargeType": 3,
                "customerName": "上海逸享云创电子商务有限公司", "customerAccount": "C202606231285",
                "buyerMemo": order.get("remark", ""),
                "tradeOrderDetails": trade_order_details,
            }
        }
        try:
            create_resp = await jky.trade_create(create_biz)
            jky_code = create_resp.get("code", -1)
            if jky_code not in (0, 200):
                logger.error(f"[cron-a] {order_no} 创单失败: {create_resp}")
                conn.execute(
                    "UPDATE order_map SET jky_state = NULL, jky_unified = NULL, bridge_unified = NULL WHERE id = ?",
                    (map_id,),
                )
                conn.commit()
                st_transition(map_id, STATE_FAILED, "cron_a", create_resp.get("msg", "创单失败"))
                continue

            jky_trade_no = (create_resp.get("result", {})
                           .get("data", {})
                           .get("tradeOrder", {})
                           .get("tradeNo", ""))
            # 假设刚创建的 JKY 单是 1010(待审核) 状态
            jky_state = "1010"
            jky_unified = jky_to_unified(jky_state)
            br_unified = bridge_to_unified(STATE_JKY_CREATED)
            conn.execute(
                """UPDATE order_map SET
                    jky_trade_no = ?, order_items_json = ?,
                    jky_state = ?, jky_unified = ?, bridge_unified = ?
                WHERE id = ?""",
                (jky_trade_no, json.dumps(products, ensure_ascii=False, default=str),
                 jky_state, jky_unified, br_unified, map_id),
            )
            conn.commit()
            logger.info(f"[cron-a] {order_no} → JKY {jky_trade_no} 创单成功 "
                        f"jky_unified={jky_unified} bridge_unified={br_unified}")
            st_transition(map_id, STATE_JKY_CREATED, "cron_a")
        except Exception as e:
            logger.error(f"[cron-a] {order_no} 创单异常: {e}")
            conn.execute(
                "UPDATE order_map SET jky_state = NULL, jky_unified = NULL, bridge_unified = NULL WHERE id = ?",
                (map_id,),
            )
            conn.commit()
            st_transition(map_id, STATE_FAILED, "cron_a", str(e))
            continue

    logger.info("[cron-a] 完成")
    set_cursor(CURSOR_KEY, now_str)
    logger.info(f"[cron-a] 游标已更新: {now_str}")
