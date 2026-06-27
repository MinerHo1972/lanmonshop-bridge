"""cron-f: 三方状态对账（中台 / 吉客云 / DB）（1/day @ 03:30 CST）

Scope 2 占位 — 仅注册调度, 业务逻辑 scope 4 实施
（中台 getDeliverOrders + 吉客云 trade_list + DB order_map 三方对比,
偏差 >5 单 → 飞书 P0, ≤5 单 → 飞书 P2 日志告警）

实施参见 PRD v0.3.6 §11.2.10
"""

import logging

from ..clients.jky import JkyClient
from ..clients.lanmonshop import LanmongClient
from ..notify.feishu import FeishuNotifier

logger = logging.getLogger(__name__)


async def run_cron_f(
    lanmong: LanmongClient,
    jky: JkyClient,
    notifier: FeishuNotifier,
) -> None:
    """scope 2 占位 — scope 4 实施真正的对账逻辑"""
    logger.info("[cron-f] 占位执行 — 业务逻辑待 scope 4 实施")
    # TODO(scope 4):
    # 1. SELECT * FROM order_map WHERE state IN ['jky_shipped','synced']
    # 2. 批量拉中台 getDeliverOrders?orderIds=...（按 50 单/批）
    # 3. 批量拉吉客云 trade_list?tradeNos=...
    # 4. 三方 state 偏差 >5 → 飞书 P0 资损告警；≤5 → 飞书 P2 日志告警
    # 5. 失败自动重试 1 次 → 仍失败 → 飞书 P1 告警（cron 自身失败）