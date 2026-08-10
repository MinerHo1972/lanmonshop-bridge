"""cron-a：中台 → 吉客云（5min）

拉蓝盟 1(待审核),2(待发货) 全量订单 → 对比 DB 刷新 platform_unified
→ 未处理订单过审 → JKY 创单 → 写 jky_unified + bridge_unified
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from ..clients.lanmonshop import LanmongClient
from ..clients.jky import JkyClient
from ..clients.jky_direct import JkyDirectClient
from ..core.state_machine import transition as st_transition, STATE_INIT, STATE_AUDITED, \
    STATE_JKY_CREATED, STATE_CANCELLED, STATE_FAILED
from ..core.exception_handler import RetryState, classify_error, Severity
from ..core.shared_unified import platform_to_unified, jky_to_unified, bridge_to_unified
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection, get_cursor, set_cursor
from ..core.sku_resolver import SkuResolver

logger = logging.getLogger(__name__)

_resolver = SkuResolver()
CURSOR_KEY = "cron_a_last_pull"
LOOKBACK_DAYS = 15


async def run_cron_a(
    lanmong: LanmongClient,
    jky: JkyClient,
    notifier: FeishuNotifier,
    auto_review: bool = True,
    jky_direct: Optional[JkyDirectClient] = None,
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
            if page == 1 and notifier:
                await notifier.alert_p1("cron-a", f"蓝盟拉单 page=1 失败: {e}", 0, 0)
            break
        if resp.get("code") != 0:
            logger.warning(f"[cron-a] 蓝盟查询异常 (page={page}): code={resp.get('code')} msg={resp.get('msg','')}")
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
        # (2026-08-10 v3.1) 蓝盟 state=2(待发货) = 已在蓝盟过审 → 仅本地 init→audited，
        # 绝不重复调 review_order（防人工重置 init 后重复过审）
        if auto_review:
            if lm_state == 2:
                # Medium-3: 检查转移结果 —— False = 订单已被其他执行者转走（并发/重入），
                # 不得继续创单（防重复 JKY 订单）
                if not st_transition(map_id, STATE_AUDITED, "cron_a"):
                    logger.warning(f"[cron-a] {order_no} init→audited 转移失败（可能已被并发处理），跳过")
                    continue
                logger.info(f"[cron-a] {order_no} 蓝盟已过审(state=2)，本地转 audited（不重复过审）")
            else:
                try:
                    review_resp = await lanmong.review_order(order_no)
                    if review_resp.get("code") != 0:
                        logger.warning(f"[cron-a] {order_no} 过审失败: {review_resp}")
                        continue
                    if not st_transition(map_id, STATE_AUDITED, "cron_a"):
                        logger.warning(f"[cron-a] {order_no} 过审后 init→audited 转移失败（可能已被并发处理），跳过")
                        continue
                    logger.info(f"[cron-a] {order_no} 过审成功")
                except Exception as e:
                    logger.error(f"[cron-a] {order_no} 过审异常: {e}")
                    st_transition(map_id, STATE_FAILED, "cron_a", str(e))
                    continue

        # ---- Step 4: 解析 SKU ----
        products = order.get("orderProducts", [])
        trade_order_details = []
        skip_order = False
        order_total = 0.0
        for item in products:
            # M-1: product_no 先统一规范化（str + strip），后续空值/YX 前缀判断都用规范化值
            product_no = str(item.get("productNo") or "").strip()
            qty = item.get("num") or item.get("number") or 1
            if not product_no:
                msg = "缺 productNo"
                logger.warning(f"[cron-a] {order_no} {msg}，跳过")
                conn.execute(
                    "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (msg, map_id),
                )
                conn.commit()
                if notifier:
                    await notifier.alert_p1(
                        "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                    )
                skip_order = True
                break

            # YX 前缀转换：正式网站 YX 编码 → sku_mapping → jky_goods_no
            jky_goods_no = str(product_no).strip()
            if product_no.startswith("YX"):
                resolved = _resolver.resolve(product_no)
                if not resolved:
                    msg = f"{product_no} 无sku映射（应补 sku_mapping 表）"
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    await notifier.alert_p1(
                        "cron-a", f"订单 {order_no} SKU缺映射: {msg}",
                        retry_count=0, order_map_id=map_id,
                    )
                    skip_order = True
                    break
                jky_goods_no = str(resolved).strip()
                logger.info(f"[cron-a] {order_no} YX映射: {product_no} → {jky_goods_no}")
            if not jky_goods_no:
                msg = f"{product_no} 解析后编码为空，无法创单"
                logger.warning(f"[cron-a] {order_no} {msg}")
                conn.execute(
                    "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (msg, map_id),
                )
                conn.commit()
                if notifier:
                    await notifier.alert_p1(
                        "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                    )
                skip_order = True
                break

            prod_row = conn.execute(
                "SELECT jky_barcode, jky_goods_name, raw_json FROM jky_product_cache WHERE jky_goods_no = ?",
                (jky_goods_no,),
            ).fetchone()
            if not prod_row:
                # ---- cache-miss 自恢复 (2026-08-10 v3.1) ----
                # 编码长度规则（用户拍板）：<15 位 = 基础商品；≥15 位 = 组合装
                code_len = len(str(jky_goods_no).strip())
                if code_len >= 15:
                    msg = f"{jky_goods_no} 组合装无缓存（编码≥15位），需人工补录 jky_product_cache"
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    if notifier:
                        await notifier.alert_p1(
                            "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                        )
                    skip_order = True
                    break

                # 基础商品：点查 erp.stockquantity.get 补档案（固定创单仓 02 + 唯一匹配校验）
                # H-1: 直连 JkyDirectClient（进程内），不走 HTTP 路由 —— 无公网暴露面
                if jky_direct is None:
                    msg = f"{jky_goods_no} jky_direct 客户端未注入，无法点查补档案，需人工补录"
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    if notifier:
                        await notifier.alert_p1(
                            "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                        )
                    skip_order = True
                    break
                logger.info(f"[cron-a] {order_no} {jky_goods_no} 基础商品 cache-miss，点查库存接口补档案")
                stock_records: list = []
                attempt = 0
                last_fail = ""
                while True:
                    attempt += 1
                    try:
                        stock_resp = await jky_direct.stockquantity_get({
                            "goodsNo": str(jky_goods_no),
                            "warehouseCode": "02",
                            "pageIndex": 0,
                            "pageSize": 5,
                        })
                        if stock_resp.get("code") != 200:
                            msg = (f"{jky_goods_no} 点查失败 code={stock_resp.get('code')} "
                                   f"msg={stock_resp.get('msg','')}（不重试，需人工补录）")
                            logger.warning(f"[cron-a] {order_no} {msg}")
                            conn.execute(
                                "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (msg, map_id),
                            )
                            conn.commit()
                            if notifier:
                                await notifier.alert_p1(
                                    "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                                )
                            skip_order = True
                            break
                        result_wrapper = stock_resp.get("result") or {}
                        data = result_wrapper.get("data") or {}
                        if isinstance(data, dict):
                            records = data.get("goodsStockQuantity") or []
                        elif isinstance(data, list):
                            records = data
                        else:
                            records = []
                        if not isinstance(records, list):
                            records = []
                        stock_records = records
                        break  # HTTP + code=200 层成功
                    except httpx.HTTPStatusError as e:
                        status = e.response.status_code if e.response is not None else 0
                        retryable = status in (408, 429) or status >= 500
                        if not retryable:
                            msg = f"{jky_goods_no} 点查 HTTP {status}（未知错误，不重试，需人工补录）"
                            logger.warning(f"[cron-a] {order_no} {msg}")
                            conn.execute(
                                "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (msg, map_id),
                            )
                            conn.commit()
                            if notifier:
                                await notifier.alert_p1(
                                    "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                                )
                            skip_order = True
                            break
                        last_fail = f"HTTP {status}"
                    except (httpx.TimeoutException, httpx.ConnectError, httpx.TransportError) as e:
                        last_fail = f"网络错误: {type(e).__name__}"
                    except Exception as e:
                        msg = f"{jky_goods_no} 点查异常: {e}（需人工补录）"
                        logger.warning(f"[cron-a] {order_no} {msg}")
                        conn.execute(
                            "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (msg, map_id),
                        )
                        conn.commit()
                        if notifier:
                            await notifier.alert_p1(
                                "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                            )
                        skip_order = True
                        break
                    if attempt >= 3:
                        msg = f"{jky_goods_no} 点查重试{attempt}次仍失败（{last_fail}），需人工补录"
                        logger.warning(f"[cron-a] {order_no} {msg}")
                        conn.execute(
                            "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (msg, map_id),
                        )
                        conn.commit()
                        if notifier:
                            await notifier.alert_p1(
                                "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                            )
                        skip_order = True
                        break
                    await asyncio.sleep(5)
                if skip_order:
                    break

                # 唯一匹配校验：恰好 1 条且 goodsNo 精确等于目标，否则 halt 不写缓存
                target_no = str(jky_goods_no).strip()
                # M-3 (2026-08-10 Codex 第三轮): 原始响应含任意非 dict 元素 = 结构非法，
                # 必须 halt（不能过滤后误判唯一）。唯一性基于原始候选而非过滤后列表。
                raw_count = len(stock_records)
                malformed = [r for r in stock_records if not isinstance(r, dict)]
                if malformed:
                    msg = (f"{jky_goods_no} 点查响应含 {len(malformed)} 条畸形记录"
                           f"（共 {raw_count} 条），结构非法，需人工补录")
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    if notifier:
                        await notifier.alert_p1(
                            "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                        )
                    skip_order = True
                    break
                matched = [
                    r for r in stock_records
                    if str((r or {}).get("goodsNo") or "").strip() == target_no
                ]
                if len(stock_records) != 1 or len(matched) != 1:
                    msg = (f"{jky_goods_no} 点查候选 {len(stock_records)} 条（匹配 {len(matched)} 条），"
                           f"无法确定性补档案，需人工补录")
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    if notifier:
                        await notifier.alert_p1(
                            "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                        )
                    skip_order = True
                    break
                rec = matched[0]
                goods_name = str(rec.get("goodsName") or "").strip()
                sku_barcode = str(rec.get("skuBarcode") or "").strip()
                raw_unit = rec.get("unitName")
                unit_name_ok = isinstance(raw_unit, str) and bool(raw_unit.strip())
                raw_obj = dict(rec)
                # High-1: unitName 必须在写缓存前校验非空 —— 否则写入不完整缓存，
                # 人工重驱时 prod_row 已存在不再点查，因缺 unitName 永久卡单
                if not goods_name or not sku_barcode or not unit_name_ok:
                    msg = (f"{jky_goods_no} 点查缺 goodsName/skuBarcode/unitName"
                           f"(name={bool(goods_name)} barcode={bool(sku_barcode)} unit={unit_name_ok})，"
                           f"不写缓存，需人工补录")
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    if notifier:
                        await notifier.alert_p1(
                            "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                        )
                    skip_order = True
                    break
                # UPSERT 缓存（ON CONFLICT 保留 created_at；网络调用期间未持有写事务）
                conn.execute(
                    """INSERT INTO jky_product_cache
                       (jky_goods_no, jky_goods_name, jky_barcode, raw_json, fetched_at)
                       VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(jky_goods_no) DO UPDATE SET
                         jky_goods_name = excluded.jky_goods_name,
                         jky_barcode = excluded.jky_barcode,
                         raw_json = excluded.raw_json,
                         fetched_at = excluded.fetched_at""",
                    (target_no, goods_name, sku_barcode, json.dumps(raw_obj, ensure_ascii=False)),
                )
                conn.commit()
                logger.info(f"[cron-a] {order_no} {target_no} 点查补档成功，写入缓存")
                # v3.1: 重新读取完整 prod_row（不得沿用点查 record 或旧 None 值）
                prod_row = conn.execute(
                    "SELECT jky_barcode, jky_goods_name, raw_json FROM jky_product_cache WHERE jky_goods_no = ?",
                    (jky_goods_no,),
                ).fetchone()
                # Medium-4: 重读后统一校验必填字段（barcode/goodsName/raw_json），
                # 防并发覆盖或 DB 异常导致不完整档案继续创单
                re_barcode = (prod_row["jky_barcode"] or "").strip() if prod_row else ""
                re_name = (prod_row["jky_goods_name"] or "").strip() if prod_row else ""
                re_raw = (prod_row["raw_json"] or "") if prod_row else ""
                if not re_barcode or not re_name or not re_raw:
                    msg = f"{jky_goods_no} 补档后重读缓存仍缺字段(barcode={bool(re_barcode)} name={bool(re_name)} raw={bool(re_raw)})，halt"
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    if notifier:
                        await notifier.alert_p1(
                            "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                        )
                    skip_order = True
                    break
            cost_price = float(item.get("costPrice", 0) or 0)
            barcode = prod_row["jky_barcode"]
            if not barcode:
                # 组合装商品允许空条码（isFit=1，直接走 goodsNo 匹配）
                is_fit_row = conn.execute(
                    "SELECT is_fit FROM sku_mapping WHERE jky_goods_no = ?",
                    (jky_goods_no,),
                ).fetchone()
                if is_fit_row and is_fit_row["is_fit"] == 1:
                    logger.info(f"[cron-a] {order_no} {jky_goods_no} 组合装(跳过条码校验)")
                else:
                    msg = f"{jky_goods_no} 条码为空（jky_product_cache 中 barcode 为空）"
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    if notifier:
                        await notifier.alert_p1(
                            "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                        )
                    skip_order = True
                    break
            # 从 JKY 商品缓存 raw_json 取 unitName（如 "瓶"/"套"/"件"），缺失/异常 → fail-closed 告警跳过
            unit_name = None
            try:
                raw = prod_row["raw_json"] or ""
                if raw:
                    raw_obj = json.loads(raw)
                    if isinstance(raw_obj, dict):
                        un = raw_obj.get("unitName")
                        if isinstance(un, str) and un.strip():
                            unit_name = un.strip()
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, ValueError):
                unit_name = None
            if not unit_name:
                # 组合装 (is_fit=1) 在 JKY 商品表无档案，raw_json 必空 → unit 兜底 "套"（与现有组合装 180202408090302785 一致）
                is_fit_row = conn.execute(
                    "SELECT is_fit FROM sku_mapping WHERE jky_goods_no = ?",
                    (jky_goods_no,),
                ).fetchone()
                if is_fit_row and is_fit_row["is_fit"] == 1:
                    unit_name = "套"
                    logger.info(f"[cron-a] {order_no} {jky_goods_no} 组合装(unit 兜底'套')")
                else:
                    msg = f"{jky_goods_no} 缓存缺 unitName（jky_product_cache raw_json），无法创单"
                    logger.warning(f"[cron-a] {order_no} {msg}")
                    conn.execute(
                        "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (msg, map_id),
                    )
                    conn.commit()
                    if notifier:
                        await notifier.alert_p1(
                            "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                        )
                    skip_order = True
                    break
            # 从 JKY 商品缓存 raw_json 取规格名 skuName（如 "1瓶装"/"50颗装"），缺失/异常 → 回退 "默认"
            # （JKY 对 specName 宽松不校验：1377 商品无一 "默认" 但历史推单全成功；unit 才严格校验）
            spec_name = "默认"
            try:
                raw = prod_row["raw_json"] or ""
                if raw:
                    raw_obj = json.loads(raw)
                    if isinstance(raw_obj, dict):
                        sn = raw_obj.get("skuName")
                        if isinstance(sn, str) and sn.strip():
                            spec_name = sn.strip()
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError, ValueError):
                spec_name = "默认"
            sell_total = round(cost_price * qty, 2)
            order_total += sell_total
            item_detail = {
                "goodsNo": jky_goods_no,
                "barcode": barcode or jky_goods_no,
                "goodsName": prod_row["jky_goods_name"] or "",
                "specName": spec_name, "unit": unit_name,
                "sellPrice": cost_price, "sellCount": qty, "sellTotal": sell_total,
            }
            # Low-2/L-1: is_fit 一致性检测 —— 同一 jky_goods_no 映射到不同 is_fit 值 = 数据冲突；
            # NULL 视为非法值（SUM(is_fit IS NULL) > 0 即 halt），不得任取一行
            is_fit_conflict = conn.execute(
                "SELECT COUNT(DISTINCT is_fit), COUNT(*) - COUNT(is_fit) "
                "FROM sku_mapping WHERE jky_goods_no = ?",
                (jky_goods_no,),
            ).fetchone()
            if is_fit_conflict and (is_fit_conflict[0] > 1 or is_fit_conflict[1] > 0):
                msg = f"{jky_goods_no} sku_mapping 存在 is_fit 冲突/空值（distinct={is_fit_conflict[0]} null={is_fit_conflict[1]}），需人工清理"
                logger.warning(f"[cron-a] {order_no} {msg}")
                conn.execute(
                    "UPDATE order_map SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (msg, map_id),
                )
                conn.commit()
                if notifier:
                    await notifier.alert_p1(
                        "cron-a", f"订单 {order_no} {msg}", retry_count=0, order_map_id=map_id,
                    )
                skip_order = True
                break
            is_fit_row = conn.execute(
                "SELECT is_fit FROM sku_mapping WHERE jky_goods_no = ?",
                (jky_goods_no,),
            ).fetchone()
            if is_fit_row and is_fit_row["is_fit"] == 1:
                item_detail["isFit"] = 1
            trade_order_details.append(item_detail)

        if skip_order:
            continue

        # ---- Step 5: JKY 创单 ----
        receiver_mobile = order.get("mobile", "")
        create_biz = {
            "tradeOrder": {
                "onlineTradeNo": order_no,
                "shopName": "特渠分销对接", "shopCode": "0125", "warehouseCode": "02",
                "tradeTime": now_str, "tradeType": 1,
                "totalFee": order_total, "payment": order_total, "chargeCurrency": "人民币",
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
            if jky_code != 200:
                logger.error(f"[cron-a] {order_no} 创单失败: {create_resp}")
                if notifier:
                    await notifier.alert_p1("cron-a", f"订单 {order_no} JKY 创单失败: {create_resp.get('msg','')} (code={jky_code})", 0, map_id)
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
            if not jky_trade_no:
                msg = f"JKY 创单返回但缺 tradeNo: {json.dumps(create_resp, ensure_ascii=False)[:200]}"
                logger.error(f"[cron-a] {order_no} {msg}")
                if notifier:
                    await notifier.alert_p0(order_no, msg, map_id, "jky_created")
                conn.execute(
                    "UPDATE order_map SET jky_state = NULL, jky_unified = NULL, bridge_unified = NULL WHERE id = ?",
                    (map_id,),
                )
                conn.commit()
                st_transition(map_id, STATE_FAILED, "cron_a", msg)
                continue
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
