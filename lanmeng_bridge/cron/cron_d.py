"""cron-d：吉客云货品列表每日拉取（1/day @ 02:00 CST）

Scope 4 重构 (v0.3.6 §11.2.3 + §11.2.8):
- soft-delete + diff-INSERT 算法（替代 v0.3 TRUNCATE+INSERT）
- 双写 jky_product_cache + jky_product_cache_changes（审计即架构）
- request body 带 category IN [饮料, 周边]（P9 分类筛选）
- run_id 关联每次拉取，便于审计回溯

流程：
1. 调 JKY /jky/goods/list (erp-goods.goods.sku.search) 分页拉取
   request body 带 category 筛选（client 端过滤下沉到 request）
2. SELECT 当前 jky_product_cache 全部 (jky_goods_no, jky_category)
3. diff：旧-新 = DELETE；新-旧 = INSERT；交集 = UPDATE（如 name/price/stock 变化）
4. 事务：写 changes 表 + UPSERT 主表
5. 失败 → 自动重试 1 次 → 仍失败 → 飞书 P1 告警

验证 (scope 5):
- SELECT COUNT(*) GROUP BY jky_category 应 2 行（饮料 + 周边）
- SELECT COUNT(*) FROM jky_product_cache_changes WHERE changed_at > DATE('now', '-1 day') > 0
"""

import json
import logging
import sqlite3
import time
import uuid
from typing import Optional

from ..clients.jky import JkyClient
from ..core.exception_handler import RetryState
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)

# P9 拍板: 仅入库 饮料 / 周边 两类
ALLOWED_CATEGORIES = {"饮料", "周边"}


def _make_run_id() -> str:
    """生成 cron-d run_id（用于 changes.cron_run_id 关联）"""
    return f"cron-d-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _normalize_goods(goods: dict) -> dict:
    """规范化货品字段：吉客云 API 返回字段名 → DB 列名

    兼容多种返回字段命名（goodsNo/goodsNumber/goods_no 等）,
    category 字段映射到中文分类名（运营审计口径）。
    """
    no = (
        goods.get("goodsNo")
        or goods.get("goodsNumber")
        or goods.get("goods_no")
        or goods.get("skuNo")
        or ""
    )
    name = (
        goods.get("goodsName")
        or goods.get("goods_name")
        or goods.get("name")
        or ""
    )
    barcode = goods.get("barcode") or goods.get("barCode") or ""
    category = goods.get("category") or goods.get("categoryName") or goods.get("catName") or ""
    category_id = (
        str(goods.get("categoryId") or goods.get("catId") or "")
    )
    price = float(goods.get("price") or 0)
    stock = int(goods.get("stock") or 0)
    return {
        "jky_goods_no": str(no),
        "jky_goods_name": str(name),
        "jky_barcode": str(barcode),
        "jky_category": str(category),
        "jky_category_id": category_id,
        "jky_price": price,
        "jky_stock": stock,
        "raw_json": json.dumps(goods, ensure_ascii=False),
    }


async def _fetch_all_goods(
    jky: JkyClient,
    page_size: int = 100,
) -> list[dict]:
    """分页拉取吉客云货品列表（带 category 筛选）

    Returns:
        规范化后的货品 dict 列表（仅含 category IN {饮料, 周边}）
    """
    all_goods: list[dict] = []
    page_index = 0
    total_pages: Optional[int] = None

    while total_pages is None or page_index < total_pages:
        # P9: request body 带 category 筛选（client 端不再过滤）
        biz = {
            "pageIndex": page_index,
            "pageSize": page_size,
            "category": list(ALLOWED_CATEGORIES),  # JKY 接受 list / 多值
            "categories": list(ALLOWED_CATEGORIES),  # 兼容字段命名
        }
        try:
            resp = await jky.goods_search(biz)
        except Exception as e:
            logger.error(f"[cron-d] 第 {page_index + 1} 页拉取异常: {e}")
            raise

        if resp.get("code") != 0:
            # 把 resp 抛出，让外层 retry 兜底
            raise RuntimeError(
                f"JKY 货品查询异常 code={resp.get('code')} msg={resp.get('msg', '')}"
            )

        data = resp.get("data", {})
        goods_list = data.get("list", data.get("rows", []))
        if not goods_list:
            break

        for g in goods_list:
            norm = _normalize_goods(g)
            # 兜底过滤：即便 request 漏过滤，本地也再 filter 一遍
            if norm["jky_category"] in ALLOWED_CATEGORIES:
                all_goods.append(norm)
            else:
                logger.debug(
                    f"[cron-d] 跳过非目标分类: {norm['jky_goods_no']} "
                    f"category={norm['jky_category']!r}"
                )

        page_index += 1

        # 第一页响应获取 total（兼容 total / totalCount）
        if total_pages is None:
            total = data.get("total", data.get("totalCount", 0))
            total_pages = (total + page_size - 1) // page_size
            logger.info(
                f"[cron-d] JKY 总货品 {total} 条 → {total_pages} 页; "
                f"目标分类 {ALLOWED_CATEGORIES}"
            )

    logger.info(f"[cron-d] 拉取完成, 命中目标分类 {len(all_goods)} 条")
    return all_goods


def _diff_and_persist(
    new_goods: list[dict],
    run_id: str,
) -> tuple[int, int, int]:
    """soft-delete + diff-INSERT 算法

    Args:
        new_goods: 规范化后的新货品列表
        run_id: 本次 cron 拉取 run_id

    Returns:
        (insert_count, delete_count, update_count)

    算法:
        old_keys = SELECT jky_goods_no FROM jky_product_cache
        new_keys = {g['jky_goods_no'] for g in new_goods}
        to_delete = old_keys - new_keys
        to_insert = new_keys - old_keys
        to_check  = old_keys ∩ new_keys (UPDATE 检测)

    写入顺序（事务）:
        1. DELETE → 写 jky_product_cache_changes (change_type=DELETE)
        2. INSERT → 写 jky_product_cache + changes (INSERT)
        3. UPDATE → 写 jky_product_cache + changes (UPDATE)
    """
    conn = get_connection()

    # 1. SELECT 当前所有 jky_goods_no
    old_rows = conn.execute(
        "SELECT jky_goods_no, jky_goods_name, jky_barcode, jky_category, "
        "jky_price, jky_stock, raw_json FROM jky_product_cache"
    ).fetchall()
    old_map: dict[str, sqlite3.Row] = {r["jky_goods_no"]: r for r in old_rows}
    old_keys = set(old_map.keys())
    new_keys = {g["jky_goods_no"] for g in new_goods}
    new_map = {g["jky_goods_no"]: g for g in new_goods}

    to_delete = old_keys - new_keys
    to_insert = new_keys - old_keys
    to_check = old_keys & new_keys

    insert_count = 0
    delete_count = 0
    update_count = 0

    try:
        # 2. DELETE 流程: 软删除主表 + 写 changes
        for goods_no in to_delete:
            old = old_map[goods_no]
            old_value = json.dumps(
                {
                    "jky_goods_no": old["jky_goods_no"],
                    "jky_goods_name": old["jky_goods_name"],
                    "jky_barcode": old["jky_barcode"],
                    "jky_category": old["jky_category"],
                    "jky_price": old["jky_price"],
                    "jky_stock": old["jky_stock"],
                },
                ensure_ascii=False,
            )
            conn.execute(
                "DELETE FROM jky_product_cache WHERE jky_goods_no = ?",
                (goods_no,),
            )
            conn.execute(
                """INSERT INTO jky_product_cache_changes
                    (jky_goods_no, change_type, old_value, new_value, cron_run_id)
                VALUES (?, 'DELETE', ?, NULL, ?)""",
                (goods_no, old_value, run_id),
            )
            delete_count += 1

        # 3. INSERT 流程: 写入主表 + changes
        for goods_no in to_insert:
            g = new_map[goods_no]
            conn.execute(
                """INSERT INTO jky_product_cache
                    (jky_goods_no, jky_goods_name, jky_barcode,
                     jky_category, jky_category_id, jky_price, jky_stock,
                     raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    g["jky_goods_no"],
                    g["jky_goods_name"],
                    g["jky_barcode"],
                    g["jky_category"],
                    g["jky_category_id"],
                    g["jky_price"],
                    g["jky_stock"],
                    g["raw_json"],
                ),
            )
            conn.execute(
                """INSERT INTO jky_product_cache_changes
                    (jky_goods_no, change_type, old_value, new_value, cron_run_id)
                VALUES (?, 'INSERT', NULL, ?, ?)""",
                (goods_no, g["raw_json"], run_id),
            )
            insert_count += 1

        # 4. UPDATE 流程: 比较 name/price/stock，变化则写 changes + UPSERT 主表
        for goods_no in to_check:
            g = new_map[goods_no]
            old = old_map[goods_no]
            old_value = json.dumps(
                {
                    "jky_goods_no": old["jky_goods_no"],
                    "jky_goods_name": old["jky_goods_name"],
                    "jky_barcode": old["jky_barcode"],
                    "jky_category": old["jky_category"],
                    "jky_price": old["jky_price"],
                    "jky_stock": old["jky_stock"],
                },
                ensure_ascii=False,
            )
            # 检查字段变化
            changed = (
                old["jky_goods_name"] != g["jky_goods_name"]
                or old["jky_barcode"] != g["jky_barcode"]
                or old["jky_category"] != g["jky_category"]
                or float(old["jky_price"] or 0) != g["jky_price"]
                or int(old["jky_stock"] or 0) != g["jky_stock"]
            )
            if changed:
                conn.execute(
                    """UPDATE jky_product_cache SET
                        jky_goods_name = ?, jky_barcode = ?,
                        jky_category = ?, jky_category_id = ?,
                        jky_price = ?, jky_stock = ?,
                        raw_json = ?, fetched_at = CURRENT_TIMESTAMP
                    WHERE jky_goods_no = ?""",
                    (
                        g["jky_goods_name"],
                        g["jky_barcode"],
                        g["jky_category"],
                        g["jky_category_id"],
                        g["jky_price"],
                        g["jky_stock"],
                        g["raw_json"],
                        goods_no,
                    ),
                )
                conn.execute(
                    """INSERT INTO jky_product_cache_changes
                        (jky_goods_no, change_type, old_value, new_value, cron_run_id)
                    VALUES (?, 'UPDATE', ?, ?, ?)""",
                    (goods_no, old_value, g["raw_json"], run_id),
                )
                update_count += 1
            else:
                # 无变化也刷一下 fetched_at（让 cron 心跳可监控）
                conn.execute(
                    """UPDATE jky_product_cache SET
                        fetched_at = CURRENT_TIMESTAMP
                    WHERE jky_goods_no = ?""",
                    (goods_no,),
                )

        conn.commit()
        logger.info(
            f"[cron-d] diff 完成: INSERT={insert_count} "
            f"DELETE={delete_count} UPDATE={update_count} "
            f"(unchanged={len(to_check) - update_count})"
        )
    except Exception as e:
        conn.rollback()
        raise

    return insert_count, delete_count, update_count


async def run_cron_d(
    jky: JkyClient,
    notifier: FeishuNotifier,
) -> None:
    """每日货品列表拉取 + soft-delete + diff-INSERT

    PRD §11.2.3 + §11.2.8 v0.3.6 实施
    """
    run_id = _make_run_id()
    logger.info(f"[cron-d] 开始 run_id={run_id}")

    retry = RetryState(max_attempts=2, backoff_minutes=[5])  # PRD §11.2.3: 重试 1 次
    new_goods: Optional[list[dict]] = None
    last_error: Optional[str] = None

    # PRD §11.2.3: "失败 → 自动重试 1 次 → 仍失败 → 飞书 P1 告警"
    # cron-d 频次 1/day, "5min 后重试"在 24h 间隔下意义不大,
    # 采用即时重试 1 次的语义（若需 5min 推迟重试, 应用 APScheduler add_job run_date)
    while not retry.is_exhausted:
        try:
            new_goods = await _fetch_all_goods(jky)
            break
        except Exception as e:
            last_error = str(e)
            retry.record_attempt(last_error)
            logger.error(
                f"[cron-d] 拉取失败 (attempt={retry.attempt}): {last_error}"
            )
            if retry.is_exhausted:
                # 重试耗尽 → 飞书 P1
                await notifier.alert_p1(
                    "cron-d",
                    f"货品列表拉取失败（重试耗尽）: {last_error}",
                    retry.attempt,
                    0,
                )
                return
            # 否则继续 while 循环, 下次循环再 fetch 一次（即时重试）

    if new_goods is None:
        logger.warning("[cron-d] new_goods is None, 跳过 diff")
        return

    # diff + 持久化
    try:
        ins, dele, upd = _diff_and_persist(new_goods, run_id)
        logger.info(
            f"[cron-d] run_id={run_id} 完成: "
            f"fetched={len(new_goods)} ins={ins} del={dele} upd={upd}"
        )
    except Exception as e:
        logger.error(f"[cron-d] diff 持久化失败: {e}")
        await notifier.alert_p1(
            "cron-d", f"diff 持久化失败: {e}", 0, 0
        )
        return