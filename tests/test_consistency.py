"""三端一致性判定单测 — classify_consistency

覆盖场景：
1. 三端严格一致 → consistent
2. 已回传：蓝盟=已发货(4) + DB/JKY=已完成 → synced_back（本次修复核心）
3. 已回传：蓝盟=已完成(6) + DB/JKY=已完成 → synced_back
4. 蓝盟滞后在待发货(2)/部分发货(3) + DB/JKY=已完成 → inconsistent（真偏差）
5. 蓝盟已取消 + DB/JKY 已完成 → inconsistent
6. 未创单/未发货阶段 → inconsistent
7. cron_f 规则语义：db done + 蓝盟 4/6 不再报偏差
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanmeng_bridge.core.consistency import (
    CONSISTENT,
    INCONSISTENT,
    SYNCED_BACK,
    classify_consistency,
    deviation_reason_for_synced_back,
    is_lanmeng_state_shipped_or_done,
)


def test_strict_consistent_all_done():
    assert classify_consistency("已完成", "已完成", "已完成", 6) == CONSISTENT


def test_strict_consistent_all_pending():
    assert classify_consistency("待发货", "待发货", "待发货", 2) == CONSISTENT


def test_synced_back_lanmeng_shipped():
    """核心场景：cron-b 回传完成，DB/JKY 已完成，蓝盟停在已发货(4)。"""
    assert classify_consistency("已发货", "已完成", "已完成", 4) == SYNCED_BACK


def test_synced_back_lanmeng_done_unified_mismatch():
    """蓝盟统一态映射缺失（如 state=4 映射异常）时按原始 state 4 放行。"""
    assert classify_consistency("待发货", "已完成", "已完成", 4) == SYNCED_BACK


def test_synced_back_lanmeng_state_6():
    assert classify_consistency("已发货", "已完成", "已完成", 6) == SYNCED_BACK


def test_inconsistent_lanmeng_pending():
    """蓝盟仍在待发货(2)，DB/JKY 却已完成 → 真偏差。"""
    assert classify_consistency("待发货", "已完成", "已完成", 2) == INCONSISTENT


def test_inconsistent_lanmeng_partial():
    assert classify_consistency("部分发货", "已完成", "已完成", 3) == INCONSISTENT


def test_inconsistent_lanmeng_cancelled():
    assert classify_consistency("已取消/退款", "已完成", "已完成", -2) == INCONSISTENT


def test_inconsistent_not_created():
    """JKY 未创建（统一态兜底值），与蓝盟待发货不等 → 不一致。"""
    assert classify_consistency("待发货", "待发货", "未创建", None) == INCONSISTENT


def test_inconsistent_jky_ahead():
    """蓝盟/DB 未发货但 JKY 已完成 → 倒挂，真偏差。"""
    assert classify_consistency("待发货", "待发货", "已完成", 2) == INCONSISTENT


def test_synced_back_requires_bridge_and_jky_done():
    """仅 JKY 已完成、bridge 未闭环 → 不算已回传。"""
    assert classify_consistency("已发货", "已发货", "已完成", 4) == INCONSISTENT


def test_helper_shipped_or_done():
    assert is_lanmeng_state_shipped_or_done(4) is True
    assert is_lanmeng_state_shipped_or_done(6) is True
    assert is_lanmeng_state_shipped_or_done("4") is True
    assert is_lanmeng_state_shipped_or_done(2) is False
    assert is_lanmeng_state_shipped_or_done(-2) is False
    assert is_lanmeng_state_shipped_or_done(None) is False


def test_reason_message():
    msg = deviation_reason_for_synced_back(4)
    assert "正常" in msg
    msg2 = deviation_reason_for_synced_back(None)
    assert "滞后" in msg2


def test_cron_f_rule2_semantics():
    """cron-f 规则 #2 语义自检：db=done + 蓝盟 4/6 → 不算偏差。
    - 蓝盟=4（已发货）→ synced_back（已回传）
    - 蓝盟=6（已完成）→ 三端严格一致 consistent
    两者都不是 inconsistent。"""
    for lm in (4, 6):
        result = classify_consistency(
            platform_to_unified(lm), "已完成", "已完成", lm
        )
        assert result != INCONSISTENT, f"蓝盟 state={lm} 不应判为不一致，得 {result}"
    assert classify_consistency(
        platform_to_unified(4), "已完成", "已完成", 4
    ) == SYNCED_BACK
    assert classify_consistency(
        platform_to_unified(6), "已完成", "已完成", 6
    ) == CONSISTENT


# 直接 import 真实映射，确保与生产一致
from lanmeng_bridge.core.shared_unified import platform_to_unified


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(
        [(k, v) for k, v in globals().items()
         if k.startswith("test_") and callable(v)]
    ):
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
