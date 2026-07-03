"""统一状态映射 — 蓝盟/JKY/Bridge → 五态统一模型

五态: 待发货, 部分发货, 已发货, 已完成, 已取消/退款
"""

# 蓝盟 state → 统一
_LANMONG_TO_UNIFIED = {
    1: "待发货",
    2: "待发货",
    3: "部分发货",
    4: "已发货",
    6: "已完成",
    -2: "已取消/退款",
    -3: "已取消/退款",
    -4: "已取消/退款",
}

# JKY tradeStatus → 统一
_JKY_TO_UNIFIED = {
    # 待发货
    "1010": "待发货", "1020": "待发货", "1030": "待发货",
    "1050": "待发货", "2000": "待发货",
    "2010": "待发货", "2020": "待发货", "2030": "待发货", "2040": "待发货",
    "4040": "待发货", "4041": "待发货",
    "4110": "待发货", "4111": "待发货", "4112": "待发货", "4113": "待发货",
    # 部分发货
    "4130": "部分发货",
    # 已发货
    "3010": "已发货", "6000": "已发货",
    # 已完成
    "9090": "已完成",
    # 已取消/退款
    "4121": "已取消/退款", "4122": "已取消/退款", "4123": "已取消/退款",
    "5010": "已取消/退款", "5020": "已取消/退款", "5030": "已取消/退款",
}

# Bridge state → 统一
_BRIDGE_TO_UNIFIED = {
    "init": "待发货",
    "audited": "待发货",
    "jky_created": "待发货",
    "failed": "待发货",
    "jky_shipped": "已发货",
    "synced": "已完成",
    "done": "已完成",
    "cancelled": "已取消/退款",
    "jky_cancelled": "已取消/退款",
    "skipped": "待发货",
    "static": "待发货",
}


def platform_to_unified(state) -> str:
    """蓝盟原始 state → 统一态"""
    if state is None:
        return "待发货"
    try:
        return _LANMONG_TO_UNIFIED.get(int(state), "待发货")
    except (ValueError, TypeError):
        return "待发货"


def jky_to_unified(trade_status) -> str:
    """JKY tradeStatus → 统一态"""
    if not trade_status:
        return "待发货"
    key = str(trade_status)
    return _JKY_TO_UNIFIED.get(key, "待发货")


def bridge_to_unified(state: str) -> str:
    """Bridge state → 统一态"""
    return _BRIDGE_TO_UNIFIED.get(state, "待发货")


_UNIFIED_PRIORITY = {
    "已取消/退款": 0,
    "待发货": 1,
    "部分发货": 2,
    "已发货": 3,
    "已完成": 4,
}


def unified_state_priority(unified_state: str) -> int:
    """统一态优先级：已完成 > 已发货 > 部分发货 > 待发货 > 已取消/退款。"""
    return _UNIFIED_PRIORITY.get(unified_state or "", _UNIFIED_PRIORITY["待发货"])


def _online_trade_parts(trade: dict) -> list[str]:
    online = str(trade.get("onlineTradeNo", "") or "")
    return [p.strip() for p in online.split(",") if p.strip()]


def _trade_no(trade: dict, fallback: str = "") -> str:
    return str(trade.get("tradeNo", "") or fallback or "")


def resolve_jky_effective_state(
    trade: dict | None,
    platform_order_no: str,
    all_jky_trades: dict,
    successor_index: dict | None = None,
) -> dict:
    """解析 JKY 原始态、有效态和关联后继单。

    5020 仍按原始映射记为「已取消/退款」。若该蓝盟单号出现在非 5020
    后继单的 onlineTradeNo 中，则有效态取所有后继单里优先级最高的一条。
    """
    trade = trade or {}
    successor_index = successor_index or {}
    raw_ts = str(trade.get("tradeStatus", "") or "")
    raw_unified = jky_to_unified(raw_ts)

    candidates = []
    if platform_order_no and platform_order_no in successor_index:
        value = successor_index[platform_order_no]
        candidates.extend(value if isinstance(value, list) else [value])

    if raw_ts == "5020" and platform_order_no:
        for key, other_trade in all_jky_trades.items():
            other_ts = str(other_trade.get("tradeStatus", "") or "")
            if other_ts == "5020":
                continue
            if platform_order_no in _online_trade_parts(other_trade):
                candidates.append(other_trade)

    seen_trade_nos = set()
    successor_trades = []
    for idx, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        tno = _trade_no(candidate, str(idx))
        if not tno or tno in seen_trade_nos:
            continue
        seen_trade_nos.add(tno)
        successor_trades.append(candidate)

    effective_trade = trade
    if successor_trades:
        effective_trade = max(
            successor_trades,
            key=lambda t: unified_state_priority(jky_to_unified(str(t.get("tradeStatus", "") or ""))),
        )

    effective_ts = str(effective_trade.get("tradeStatus", "") or raw_ts)
    effective_unified = jky_to_unified(effective_ts)
    related_order_nos = sorted({
        part
        for successor in successor_trades
        for part in _online_trade_parts(successor)
    })

    return {
        "raw_ts": raw_ts,
        "raw_unified": raw_unified,
        "effective_ts": effective_ts,
        "effective_unified": effective_unified,
        "effective_trade": effective_trade,
        "successor_trade_nos": [_trade_no(t) for t in successor_trades if _trade_no(t)],
        "related_order_nos": related_order_nos,
    }


def find_split_merge_successor(
    trade: dict,
    platform_order_no: str,
    all_jky_trades: dict,
) -> list[str]:
    """检测 JKY trade 是否因拆合单而非真正取消.

    场景：JKY 合并/拆分后，原单 tradeStatus=5020（已取消-被合并），
    但这是 JKY 内部操作，不是客户取消。后继单的 onlineTradeNo 字段
    会包含所有原单号（逗号分隔）。

    Args:
        trade: 当前待检测的 JKY trade
        platform_order_no: 蓝盟订单号（即 JKY onlineTradeNo）
        all_jky_trades: 当前批次所有 JKY trade 的 {key: trade} 字典

    Returns:
        后继单 tradeNo 列表，空列表表示无后继单（真正取消）
    """
    ts = str(trade.get("tradeStatus", ""))
    if ts != "5020":
        return []  # 非取消态不需要检测

    successors = []
    for key, other_trade in all_jky_trades.items():
        other_ts = str(other_trade.get("tradeStatus", ""))
        if other_ts == "5020":
            continue  # 取消态不可能是后继单
        online = other_trade.get("onlineTradeNo", "")
        parts = [p.strip() for p in online.split(",") if p.strip()]
        if platform_order_no in parts:
            trade_no = str(other_trade.get("tradeNo", "") or key)
            if trade_no:
                successors.append(trade_no)

    return successors
