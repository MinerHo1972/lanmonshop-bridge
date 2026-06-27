"""订单状态机 — 6 态 + 审计日志"""

from typing import Optional

from ..storage.db import get_connection

# ---------- 状态定义 ----------

STATE_INIT = "init"               # 初始态（cron-a 刚发现）
STATE_AUDITED = "audited"         # 中台已自动过审
STATE_JKY_CREATED = "jky_created" # 吉客云销售单已创建 + 审核
STATE_JKY_SHIPPED = "jky_shipped" # 吉客云已发货（监听到物流单号）
STATE_SYNCED = "synced"           # 物流已回传中台
STATE_DONE = "done"               # 最终态（闭环）

# 异常态
STATE_FAILED = "failed"           # 回传失败
STATE_SKIPPED = "skipped"         # SKU 缺映射跳过
STATE_JKY_CANCELLED = "jky_cancelled"  # 中台退 → 吉客云取消
STATE_CANCELLED = "cancelled"     # 中台 -2/-3/-4 不进主流程

# ---------- 状态转移规则 ----------

# {from_state: [to_state, ...]}
_ALLOWED_TRANSITIONS = {
    STATE_INIT: [STATE_AUDITED, STATE_SKIPPED, STATE_CANCELLED, STATE_FAILED],
    STATE_AUDITED: [STATE_JKY_CREATED, STATE_FAILED],
    STATE_JKY_CREATED: [STATE_JKY_SHIPPED, STATE_FAILED, STATE_JKY_CANCELLED],
    STATE_JKY_SHIPPED: [STATE_SYNCED, STATE_FAILED],
    STATE_SYNCED: [STATE_DONE, STATE_FAILED],
    STATE_DONE: [],
    STATE_FAILED: [STATE_INIT, STATE_AUDITED, STATE_JKY_CREATED],  # 重试恢复
    STATE_SKIPPED: [],
    STATE_JKY_CANCELLED: [],
    STATE_CANCELLED: [],
}

# 终态不可再跳转
_TERMINAL_STATES = {STATE_DONE, STATE_SKIPPED, STATE_JKY_CANCELLED, STATE_CANCELLED}


def is_terminal(state: str) -> bool:
    return state in _TERMINAL_STATES


def can_transition(from_state: str, to_state: str) -> bool:
    """检查状态转移是否合法"""
    allowed = _ALLOWED_TRANSITIONS.get(from_state, [])
    return to_state in allowed


def transition(
    order_map_id: int,
    to_state: str,
    source: str,
    error: Optional[str] = None,
) -> bool:
    """执行状态转移 + 审计日志写入（原子操作）

    Args:
        order_map_id: 订单记录 ID
        to_state: 目标状态
        source: 触发源（cron_a / cron_b / cron_c / manual / api / human_close）
        error: 错误详情（NULL = 正常）

    Returns:
        True=成功, False=非法转移
    """
    conn = get_connection()

    # 读当前状态
    row = conn.execute(
        "SELECT state FROM order_map WHERE id = ?", (order_map_id,)
    ).fetchone()
    if row is None:
        return False

    from_state = row["state"]

    if not can_transition(from_state, to_state):
        return False

    # 原子更新 + 审计
    conn.execute(
        """UPDATE order_map SET
            state = ?,
            last_attempt_at = CURRENT_TIMESTAMP,
            last_error = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?""",
        (to_state, error, order_map_id),
    )
    conn.execute(
        """INSERT INTO order_status_log
            (order_map_id, from_state, to_state, source, error)
        VALUES (?, ?, ?, ?, ?)""",
        (order_map_id, from_state, to_state, source, error),
    )
    conn.commit()
    return True
