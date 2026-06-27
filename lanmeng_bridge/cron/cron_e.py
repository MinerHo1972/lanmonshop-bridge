"""cron-e: 吉客云物流公司列表每日拉取（1/day @ 02:30 CST）

Scope 2 占位 — 仅注册调度, 业务逻辑 scope 4 实施
（每日全量刷新 jky_logistic_cache + 软删除+diff-INSERT 算法 + jky_logistic_cache_changes 审计写入）

实施参见 PRD v0.3.6 §11.2.7 + §11.2.8
"""

import logging

from ..clients.jky import JkyClient
from ..notify.feishu import FeishuNotifier

logger = logging.getLogger(__name__)


async def run_cron_e(
    jky: JkyClient,
    notifier: FeishuNotifier,
) -> None:
    """scope 2 占位 — scope 4 实施真正的拉取逻辑"""
    logger.info("[cron-e] 占位执行 — 业务逻辑待 scope 4 实施")
    # TODO(scope 4):
    # 1. 分页拉取 JKY /jky/logistic/list（erp.logistic.get）
    # 2. soft-delete + diff-INSERT 算法写入 jky_logistic_cache
    # 3. 同步写 jky_logistic_cache_changes（INSERT/DELETE/UPDATE 三类）
    # 4. 失败自动重试 1 次（5min 后）→ 仍失败 → 飞书 P1 告警