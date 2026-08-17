"""三端一致性判定 — 严格一致 / 已回传 / 不一致

背景（2026-08-17）：
cron-b 完成 JKY 订单发货回传（syncOrderExpress）后，DB 状态机推进到
synced→done（统一态"已完成"），JKY 侧也是"已完成"；但蓝盟后台的订单
state 由蓝盟方自行维护和更新，通常长期停留在「已发货(4)」，部分会更新
到「已完成(6)」。旧的对账判定要求三端统一态严格相等，导致这批本属
正常的长尾订单被大量误标为"不一致"。

判定语义：
- consistent    三端统一态严格一致（真·一致）
- synced_back   已回传：DB/JKY 已闭环（已完成），蓝盟停留在
                已发货(4) 或 已完成(6) —— 正常状态，不是异常
- inconsistent  其余情况（含未创单、状态倒挂、蓝盟已取消未同步等）

蓝盟 state 含义：1 待审核 2 待发货 3 部分发货 4 已发货 6 已完成
                 -2 已取消 -3 已退款 -4 已作废
"""

from .shared_unified import (
    bridge_to_unified,
    jky_to_unified,
    platform_to_unified,
    unified_state_priority,
)

# 一致性三值
CONSISTENT = "consistent"
SYNCED_BACK = "synced_back"
INCONSISTENT = "inconsistent"

# 蓝盟「已发货/已完成」原始 state
_LANMENG_SHIPPED_OR_DONE = {4, 6}


def classify_consistency(
    platform_unified: str,
    bridge_unified: str,
    jky_unified: str,
    platform_state_raw=None,
) -> str:
    """三端统一态 → 一致性三值。

    Args:
        platform_unified: 蓝盟统一态（五态模型）
        bridge_unified:   Bridge/DB 统一态
        jky_unified:      JKY 有效统一态（优先 jky_effective_unified）
        platform_state_raw: 蓝盟原始 state（int/str，可选）。用于精确识别
            「已回传」场景——统一态映射不可逆，这里以原始 state 4/6 为准。

    Returns:
        CONSISTENT | SYNCED_BACK | INCONSISTENT
    """
    # 蓝盟侧原始态（优先），解析失败回退统一态映射
    try:
        lm_state = int(platform_state_raw) if platform_state_raw is not None else None
    except (TypeError, ValueError):
        lm_state = None

    # 1) 严格一致
    if platform_unified == bridge_unified == jky_unified:
        return CONSISTENT

    # 2) 已回传：DB/JKY 已闭环，蓝盟由对方维护、停留在已发货/已完成
    #    bridge 与 JKY 必须同为「已完成」（闭环），蓝盟可为 已发货/已完成
    if (
        bridge_unified == "已完成"
        and jky_unified == "已完成"
        and (
            platform_unified in ("已发货", "已完成")
            or (lm_state is not None and lm_state in _LANMENG_SHIPPED_OR_DONE)
        )
    ):
        return SYNCED_BACK

    # 3) 其余均视为不一致（真异常）
    return INCONSISTENT


def is_lanmeng_state_shipped_or_done(platform_state_raw) -> bool:
    """蓝盟原始 state 是否属于 已发货(4)/已完成(6)。"""
    try:
        return int(platform_state_raw) in _LANMENG_SHIPPED_OR_DONE
    except (TypeError, ValueError):
        return False


def deviation_reason_for_synced_back(platform_state_raw) -> str:
    """已回传场景下的人类可读说明（供对账日报/后台展示）。"""
    if is_lanmeng_state_shipped_or_done(platform_state_raw):
        return "已回传：DB/JKY 已完成，蓝盟侧状态由对方维护（正常）"
    return "已回传：DB/JKY 已完成，蓝盟侧状态滞后（正常长尾）"
