"""cron-e：吉客云物流公司列表每日拉取（1/day @ 02:30 CST）

Scope 4 实施 (v0.3.6 §11.2.7 + §11.2.8):
- soft-delete + diff-INSERT 算法（同 cron-d）
- 双写 jky_logistic_cache + jky_logistic_cache_changes
- API: JKY POST /jky/logistic/list → method = `erp.logistic.get`（P5 已订阅）
- run_id 关联每次拉取

流程：
1. 分页拉取 JKY 物流公司列表
2. SELECT 当前所有 jky_logistic_no（从 jky_logistic_cache）
3. diff：旧-新 = DELETE；新-旧 = INSERT；交集 = UPDATE（如 name 变化）
4. 事务：写 changes + UPSERT 主表
5. 失败 → 自动重试 1 次 → 仍失败 → 飞书 P1 告警

验证 (scope 5):
- SELECT COUNT(*) FROM jky_logistic_cache < 200（吉客云物流公司总数 <200）
- SELECT COUNT(*) FROM jky_logistic_cache_changes WHERE changed_at > DATE('now', '-1 day') > 0
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


def _make_run_id() -> str:
    """生成 cron-e run_id"""
    return f"cron-e-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _normalize_logistic(item: dict) -> dict:
    """规范化物流公司字段

    兼容多种返回字段命名:
    - logisticNo / logisticCompanyCode / code / codeValue
    - logisticName / logisticCompanyName / name / codeName
    """
    no = (
        item.get("logisticNo")
        or item.get("logisticCompanyCode")
        or item.get("code")
        or item.get("codeValue")
        or ""
    )
    name = (
        item.get("logisticName")
        or item.get("logisticCompanyName")
        or item.get("name")
        or item.get("codeName")
        or ""
    )
    return {
        "jky_logistic_no": str(no),
        "jky_logistic_name": str(name),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


async def _fetch_all_logistics(
    jky: JkyClient,
    page_size: int = 200,
) -> list[dict]:
    """分页拉取吉客云物流公司列表

    Returns:
        规范化后的物流公司 dict 列表
    """
    all_items: list[dict] = []
    page_index = 0
    total_pages: Optional[int] = None

    while total_pages is None or page_index < total_pages:
        biz = {
            "pageIndex": page_index,
            "pageSize": page_size,
        }
        try:
            resp = await jky.logistic_list(biz)
        except Exception as e:
            logger.error(f"[cron-e] 第 {page_index + 1} 页拉取异常: {e}")
            raise

        if resp.get("code") != 0:
            raise RuntimeError(
                f"JKY 物流查询异常 code={resp.get('code')} msg={resp.get('msg', '')}"
            )

        data = resp.get("data", {})
        # 兼容 data 是 dict 或 list
        if isinstance(data, dict):
            items = data.get("list", data.get("rows", []))
        elif isinstance(data, list):
            items = data
        else:
            items = []

        if not items:
            break

        for it in items:
            norm = _normalize_logistic(it)
            # 必须有 logistic_no
            if norm["jky_logistic_no"]:
                all_items.append(norm)

        page_index += 1

        # 第一页响应获取 total
        if total_pages is None:
            total = data.get("total", data.get("totalCount", 0)) if isinstance(data, dict) else 0
            total_pages = (total + page_size - 1) // page_size if total else 1
            logger.info(f"[cron-e] JKY 总物流公司 {total} → {total_pages} 页")

    logger.info(f"[cron-e] 拉取完成, 共 {len(all_items)} 条物流公司")
    return all_items


def _diff_and_persist(
    new_items: list[dict],
    run_id: str,
) -> tuple[int, int, int]:
    """soft-delete + diff-INSERT 算法（同 cron-d）

    Returns:
        (insert_count, delete_count, update_count)
    """
    conn = get_connection()

    old_rows = conn.execute(
        "SELECT jky_logistic_no, jky_logistic_name, raw_json "
        "FROM jky_logistic_cache"
    ).fetchall()
    old_map: dict[str, sqlite3.Row] = {r["jky_logistic_no"]: r for r in old_rows}
    old_keys = set(old_map.keys())
    new_keys = {item["jky_logistic_no"] for item in new_items}
    new_map = {item["jky_logistic_no"]: item for item in new_items}

    to_delete = old_keys - new_keys
    to_insert = new_keys - old_keys
    to_check = old_keys & new_keys

    insert_count = 0
    delete_count = 0
    update_count = 0

    try:
        # DELETE 流程
        for logistic_no in to_delete:
            old = old_map[logistic_no]
            old_value = json.dumps(
                {
                    "jky_logistic_no": old["jky_logistic_no"],
                    "jky_logistic_name": old["jky_logistic_name"],
                },
                ensure_ascii=False,
            )
            conn.execute(
                "DELETE FROM jky_logistic_cache WHERE jky_logistic_no = ?",
                (logistic_no,),
            )
            conn.execute(
                """INSERT INTO jky_logistic_cache_changes
                    (jky_logistic_no, change_type, old_value, new_value, cron_run_id)
                VALUES (?, 'DELETE', ?, NULL, ?)""",
                (logistic_no, old_value, run_id),
            )
            delete_count += 1

        # INSERT 流程
        for logistic_no in to_insert:
            item = new_map[logistic_no]
            conn.execute(
                """INSERT INTO jky_logistic_cache
                    (jky_logistic_no, jky_logistic_name, raw_json, fetched_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
                (item["jky_logistic_no"], item["jky_logistic_name"], item["raw_json"]),
            )
            conn.execute(
                """INSERT INTO jky_logistic_cache_changes
                    (jky_logistic_no, change_type, old_value, new_value, cron_run_id)
                VALUES (?, 'INSERT', NULL, ?, ?)""",
                (logistic_no, item["raw_json"], run_id),
            )
            insert_count += 1

        # UPDATE 流程（仅 name 字段变化）
        for logistic_no in to_check:
            item = new_map[logistic_no]
            old = old_map[logistic_no]
            if old["jky_logistic_name"] != item["jky_logistic_name"]:
                old_value = json.dumps(
                    {
                        "jky_logistic_no": old["jky_logistic_no"],
                        "jky_logistic_name": old["jky_logistic_name"],
                    },
                    ensure_ascii=False,
                )
                conn.execute(
                    """UPDATE jky_logistic_cache SET
                        jky_logistic_name = ?,
                        raw_json = ?,
                        fetched_at = CURRENT_TIMESTAMP
                    WHERE jky_logistic_no = ?""",
                    (item["jky_logistic_name"], item["raw_json"], logistic_no),
                )
                conn.execute(
                    """INSERT INTO jky_logistic_cache_changes
                        (jky_logistic_no, change_type, old_value, new_value, cron_run_id)
                    VALUES (?, 'UPDATE', ?, ?, ?)""",
                    (logistic_no, old_value, item["raw_json"], run_id),
                )
                update_count += 1
            else:
                # 无变化刷一下 fetched_at
                conn.execute(
                    """UPDATE jky_logistic_cache SET
                        fetched_at = CURRENT_TIMESTAMP
                    WHERE jky_logistic_no = ?""",
                    (logistic_no,),
                )

        conn.commit()
        logger.info(
            f"[cron-e] diff 完成: INSERT={insert_count} "
            f"DELETE={delete_count} UPDATE={update_count} "
            f"(unchanged={len(to_check) - update_count})"
        )
    except Exception as e:
        conn.rollback()
        raise

    return insert_count, delete_count, update_count


async def run_cron_e(
    jky: JkyClient,
    notifier: FeishuNotifier,
) -> None:
    """每日物流公司列表拉取 + soft-delete + diff-INSERT

    PRD §11.2.7 + §11.2.8 v0.3.6 实施
    """
    run_id = _make_run_id()
    logger.info(f"[cron-e] 开始 run_id={run_id}")

    retry = RetryState(max_attempts=2, backoff_minutes=[5])
    new_items: Optional[list[dict]] = None
    last_error: Optional[str] = None

    # 同 cron-d: 即时重试 1 次（24h 间隔下 5min 推迟意义不大）
    while not retry.is_exhausted:
        try:
            new_items = await _fetch_all_logistics(jky)
            break
        except Exception as e:
            last_error = str(e)
            retry.record_attempt(last_error)
            logger.error(
                f"[cron-e] 拉取失败 (attempt={retry.attempt}): {last_error}"
            )
            if retry.is_exhausted:
                await notifier.alert_p1(
                    "cron-e",
                    f"物流公司列表拉取失败（重试耗尽）: {last_error}",
                    retry.attempt,
                    0,
                )
                return

    if new_items is None:
        logger.warning("[cron-e] new_items is None, 跳过 diff")
        return

    # diff + 持久化
    try:
        ins, dele, upd = _diff_and_persist(new_items, run_id)
        logger.info(
            f"[cron-e] run_id={run_id} 完成: "
            f"fetched={len(new_items)} ins={ins} del={dele} upd={upd}"
        )
    except Exception as e:
        logger.error(f"[cron-e] diff 持久化失败: {e}")
        await notifier.alert_p1(
            "cron-e", f"diff 持久化失败: {e}", 0, 0
        )
        return