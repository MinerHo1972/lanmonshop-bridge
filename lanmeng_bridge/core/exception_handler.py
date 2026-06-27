"""异常处理 — P0/P1/P2 分级 + 重试 + 飞书告警"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


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


# ---------- P2 → P1 升级检测 ----------

class P2UpgradeTracker:
    """30min 滑动窗口内连续 3 次 P2 → 升级 P1"""

    def __init__(self, window_minutes: int = 30, threshold: int = 3):
        self.window_seconds = window_minutes * 60
        self.threshold = threshold
        self._events: list[float] = []
        self._upgraded: bool = False

    def record(self) -> bool:
        """记录一次 P2 事件，返回是否触发升级"""
        now = time.time()
        # 清理窗口外的记录
        self._events = [t for t in self._events if now - t < self.window_seconds]
        self._events.append(now)

        if not self._upgraded and len(self._events) >= self.threshold:
            self._upgraded = True
            return True  # 触发 P2 → P1 升级
        return False

    @property
    def count(self) -> int:
        now = time.time()
        return sum(1 for t in self._events if now - t < self.window_seconds)


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
