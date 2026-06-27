"""异常处理 — P0/P1/P2 分级 + 重试 + 飞书告警 + alert_counter 滑动窗口

Scope 4 实施 (PRD §11.2.9 + §9.2):
- alert_counter 表持久化 P2 事件计数 (替代 v0.3 in-memory P2UpgradeTracker)
- 30min 滑动窗口内连续 3 次 P2 → 升级 P1
- 升级后 1h 内不再降回 P2 (cooldown)
- exception_class 维度聚合 (e.g. 'JKYRateLimitError')
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class Severity(Enum):
    P0 = "P0"  # 资损风险 / 整链不可用
    P1 = "P1"  # 单订单卡住
    P2 = "P2"  # 偶发，自动恢复


@dataclass
class RetryState:
    """重试状态跟踪"""
    max_attempts: int = 3
    backoff_minutes: list = field(default_factory=lambda: [1, 5, 15])
    attempt: int = 0
    last_error: Optional[str] = None

    @property
    def is_exhausted(self) -> bool:
        return self.attempt >= self.max_attempts

    def next_backoff_seconds(self) -> int:
        if self.attempt < len(self.backoff_minutes):
            return self.backoff_minutes[self.attempt] * 60
        return self.backoff_minutes[-1] * 60

    def record_attempt(self, error: str):
        self.attempt += 1
        self.last_error = error


# ---------- P2 → P1 升级检测 (DB-backed via alert_counter) ----------

# PRD §11.2.9 / §9.2 配置
WINDOW_MINUTES = 30          # 滑动窗口长度
THRESHOLD = 3                # 触发升级的窗口内事件数
UPGRADE_COOLDOWN_SECONDS = 3600  # 升级后 1h 内不降回 P2


class P2UpgradeTracker:
    """P2 → P1 升级检测（DB-backed via alert_counter 表）

    接口保持兼容旧 in-memory 版本 (record() / count), 内部走 SQLite:
    - record(exception_class, last_error) → bool (是否触发升级)
    - count(exception_class) → int (当前窗口内事件数)
    - is_upgraded(exception_class) → bool (升级状态 + cooldown 检查)
    - reset(exception_class) → None (测试或人工重置用)

    状态机:
        1. 首次 P2 → window_start_ts=now, count=1, upgraded=NULL
        2. 30min 内第 2 次 P2 → count=2, upgraded=NULL
        3. 30min 内第 3 次 P2 → count=3, upgraded_to_p1_at=now → 触发升级
        4. 升级后 1h 内 (cooldown): record() 返回 True 但不重置 count
        5. cooldown 过期后再来 P2: 重置 window_start_ts=now, count=1, upgraded=NULL
    """

    def __init__(
        self,
        window_minutes: int = WINDOW_MINUTES,
        threshold: int = THRESHOLD,
        cooldown_seconds: int = UPGRADE_COOLDOWN_SECONDS,
    ):
        self.window_seconds = window_minutes * 60
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds

    def _get_row(self, exception_class: str) -> Optional[dict]:
        """从 alert_counter 表读 row"""
        from ..storage.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT exception_class, window_start_ts, count, last_error, "
            "upgraded_to_p1_at FROM alert_counter WHERE exception_class = ?",
            (exception_class,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _upsert_row(
        self,
        exception_class: str,
        window_start_ts: int,
        count: int,
        last_error: str,
        upgraded_to_p1_at: Optional[str],
    ) -> None:
        """UPSERT alert_counter row"""
        from ..storage.db import get_connection
        conn = get_connection()
        conn.execute(
            """INSERT INTO alert_counter
                (exception_class, window_start_ts, count, last_error, upgraded_to_p1_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(exception_class) DO UPDATE SET
                window_start_ts = excluded.window_start_ts,
                count = excluded.count,
                last_error = excluded.last_error,
                upgraded_to_p1_at = excluded.upgraded_to_p1_at""",
            (exception_class, window_start_ts, count, last_error, upgraded_to_p1_at),
        )
        conn.commit()

    def _is_in_cooldown(self, upgraded_to_p1_at: Optional[str]) -> bool:
        """检查是否在升级后 cooldown 期（1h 内）"""
        if upgraded_to_p1_at is None:
            return False
        try:
            # SQLite CURRENT_TIMESTAMP 格式: 'YYYY-MM-DD HH:MM:SS'
            # 转 unix timestamp
            from datetime import datetime
            dt = datetime.strptime(upgraded_to_p1_at, "%Y-%m-%d %H:%M:%S")
            upgraded_unix = dt.timestamp()
            return (time.time() - upgraded_unix) < self.cooldown_seconds
        except (ValueError, TypeError):
            return False

    def is_upgraded(self, exception_class: str) -> bool:
        """是否在 P1 升级状态（含 cooldown）"""
        row = self._get_row(exception_class)
        if row is None:
            return False
        return self._is_in_cooldown(row.get("upgraded_to_p1_at"))

    def count(self, exception_class: str) -> int:
        """当前 30min 窗口内事件数"""
        row = self._get_row(exception_class)
        if row is None:
            return 0
        now = time.time()
        window_start = row["window_start_ts"]
        # 窗口已过期 → 视为 0
        if now - window_start >= self.window_seconds:
            return 0
        return row["count"]

    def record(self, exception_class: str, last_error: str = "") -> bool:
        """记录一次 P2 事件, 返回是否触发升级

        行为:
        - 新类 / cooldown 过期后: 重置 window, count=1, 不触发升级
        - 窗口内累积: count += 1, 达 threshold 时升级 (返回 True)
        - 升级后 cooldown 内: 不重置, 不再触发升级 (但仍返回 True 表示已升级)
        """
        row = self._get_row(exception_class)
        now = time.time()

        if row is None:
            # 全新 exception_class: 初始化
            self._upsert_row(
                exception_class,
                window_start_ts=int(now),
                count=1,
                last_error=last_error,
                upgraded_to_p1_at=None,
            )
            logger.debug(
                f"[P2UpgradeTracker] {exception_class} 首次记录 (count=1)"
            )
            return False

        # 已升级且在 cooldown 内 → 不重置, 直接返回 True (已升级)
        if self._is_in_cooldown(row.get("upgraded_to_p1_at")):
            # 更新 last_error 便于审计
            self._upsert_row(
                exception_class,
                window_start_ts=row["window_start_ts"],
                count=row["count"] + 1,
                last_error=last_error,
                upgraded_to_p1_at=row["upgraded_to_p1_at"],
            )
            return True

        # 窗口过期 → 重置
        if now - row["window_start_ts"] >= self.window_seconds:
            self._upsert_row(
                exception_class,
                window_start_ts=int(now),
                count=1,
                last_error=last_error,
                upgraded_to_p1_at=None,
            )
            logger.debug(
                f"[P2UpgradeTracker] {exception_class} 窗口过期, 重置 (count=1)"
            )
            return False

        # 窗口内累积
        new_count = row["count"] + 1
        upgraded = new_count >= self.threshold
        upgraded_at = None
        if upgraded and row.get("upgraded_to_p1_at") is None:
            from datetime import datetime
            upgraded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._upsert_row(
            exception_class,
            window_start_ts=row["window_start_ts"],
            count=new_count,
            last_error=last_error,
            upgraded_to_p1_at=upgraded_at if upgraded_at else row.get("upgraded_to_p1_at"),
        )

        if upgraded:
            logger.warning(
                f"[P2UpgradeTracker] {exception_class} P2→P1 升级触发 "
                f"(count={new_count}/{self.threshold})"
            )
        return upgraded

    def reset(self, exception_class: str) -> None:
        """重置某 exception_class 的窗口（人工 / 测试用）"""
        from ..storage.db import get_connection
        conn = get_connection()
        conn.execute(
            "DELETE FROM alert_counter WHERE exception_class = ?",
            (exception_class,),
        )
        conn.commit()
        logger.info(f"[P2UpgradeTracker] {exception_class} 已重置")


# ---------- 异常分类 ----------

def classify_error(error: str, retry_state: RetryState) -> Severity:
    """根据错误信息和重试状态分类异常等级"""
    err_lower = error.lower()

    # P0 — 资损风险
    p0_keywords = [
        "货已发", "已发货但", "已签收但",
        "credential", "泄露", "auth fail",
    ]
    if any(k in err_lower for k in p0_keywords):
        return Severity.P0

    # P1 — 重试耗尽
    if retry_state.is_exhausted:
        return Severity.P1

    # P1 — SKU 缺映射
    if "sku" in err_lower and ("not found" in err_lower or "missing" in err_lower or "缺映射" in err_lower):
        return Severity.P1

    # P2 — 网络/超时/rate limit（自动恢复）
    p2_keywords = [
        "timeout", "connection", "rate limit", "429",
        "busy", "lock", "temporarily",
    ]
    if any(k in err_lower for k in p2_keywords):
        return Severity.P2

    # 其他 → 根据重试次数升级
    if retry_state.attempt >= 2:
        return Severity.P1
    return Severity.P2