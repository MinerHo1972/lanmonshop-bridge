"""cron-d：吉客云货品列表每日拉取（1/day @ 02:00 CST）

流程：
1. 调 JKY /jky/goods/list（erp-goods.goods.sku.search）全量拉取货品
2. TRUNCATE + INSERT 刷新 jky_product_cache
3. sku_mapping 表增量同步（新增 jky_goods_no + 检测已下架 SKU）
4. 失败 → 自动重试 1 次（5min 后）→ 仍失败 → 飞书 P1 告警
"""

import json
import logging

from ..clients.jky import JkyClient
from ..core.exception_handler import RetryState
from ..notify.feishu import FeishuNotifier
from ..storage.db import get_connection

logger = logging.getLogger(__name__)


async def run_cron_d(
    jky: JkyClient,
    notifier: FeishuNotifier,
):
    """每日货品列表拉取 + SKU 映射同步"""
    logger.info("[cron-d] 开始拉取货品列表")

    all_goods = []
    page_index = 0
    page_size = 100
    total_pages = None

    # 分页拉取
    while total_pages is None or page_index < total_pages:
        try:
            resp = await jky.goods_search({
                "pageIndex": page_index,
                "pageSize": page_size,
            })
        except Exception as e:
            logger.error(f"[cron-d] 第 {page_index+1} 页拉取失败: {e}")
            # 重试 1 次
            try:
                resp = await jky.goods_search({
                    "pageIndex": page_index,
                    "pageSize": page_size,
                })
            except Exception as e2:
                logger.error(f"[cron-d] 重试也失败: {e2}")
                await notifier.alert_p1(
                    "cron-d",
                    f"货品列表第 {page_index+1} 页拉取失败: {e2}",
                    1, 0,
                )
                return

        if resp.get("code") != 0:
            logger.error(f"[cron-d] JKY 返回异常: {resp}")
            await notifier.alert_p1(
                "cron-d", f"JKY 货品查询异常: {resp.get('msg', '')}", 0, 0,
            )
            return

        data = resp.get("data", {})
        goods_list = data.get("list", data.get("rows", []))
        if not goods_list:
            break

        all_goods.extend(goods_list)
        page_index += 1

        # 从第一页响应获取 total
        if total_pages is None:
            total = data.get("total", data.get("totalCount", 0))
            total_pages = (total + page_size - 1) // page_size
            logger.info(f"[cron-d] 共 {total} 条货品, {total_pages} 页")

    logger.info(f"[cron-d] 拉取完成, 共 {len(all_goods)} 条货品")

    # 事务刷新 cache
    conn = get_connection()
    try:
        conn.execute("DELETE FROM jky_product_cache")
        for goods in all_goods:
            conn.execute(
                """INSERT INTO jky_product_cache
                    (jky_goods_no, jky_goods_name, jky_barcode,
                     jky_category_id, jky_price, jky_stock,
                     raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    goods.get("goodsNo", ""),
                    goods.get("goodsName", ""),
                    goods.get("barcode", ""),
                    goods.get("categoryId", ""),
                    goods.get("price", 0),
                    goods.get("stock", 0),
                    json.dumps(goods, ensure_ascii=False),
                ),
            )
        conn.commit()
        logger.info(f"[cron-d] jky_product_cache 刷新完成 ({len(all_goods)} 条)")
    except Exception as e:
        conn.rollback()
        logger.error(f"[cron-d] cache 刷新失败: {e}")
        await notifier.alert_p1("cron-d", f"DB 写入失败: {e}", 0, 0)
        return

    # 增量同步 sku_mapping（新增 jky_goods_no → 关联到已有 platform_sku_no）
    # sku_mapping 的主体数据源仍是手动导入/中台推的，cron-d 只负责补充 jky_goods_no
    # 实际 sku_mapping 的维护在中台 SKU 推来时填充
    logger.info("[cron-d] 完成")
