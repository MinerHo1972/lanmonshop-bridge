#!/usr/bin/env python3
"""init_logistic.py — cron-e 失败时人工应急同步脚本（PRD §11.2.4 + §11.2.7）

Scope 4 子项 (v0.3.6): 当 cron-e 自动拉取失败时, 运营可手动执行此脚本
一次性拉取吉客云物流公司列表并写入 jky_logistic_cache (+ 审计表).

复用策略:
- 不重写 soft-delete + diff-INSERT 算法
- 直接调 cron_e.run_cron_e(jky, notifier) — 该函数已包含重试 + 告警 + 持久化全套逻辑
- 仅在 stdout 输出 SOP §2.2 期望的可读格式 (供运营对账)

CLI:
    python3 scripts/init_logistic.py --manual --source jky --target jky_logistic_cache

退出码:
- 0: 成功 (fetch + diff 完成)
- 1: 失败 (fetch 失败 / diff 失败 / 凭证缺失)

不做:
- 不注册 systemd (应急脚本不抢 cron 的 SQLite 写锁)
- 不依赖 APScheduler (脚本独立可执行)
- 不修改 cron_e.py (代码复用优先于脚本独立重写)
"""
import argparse
import asyncio
import sys
from pathlib import Path

# 允许 scripts/ 直接 import lanmeng_bridge (项目根加到 sys.path)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lanmeng_bridge.clients.jky import create_jky_client
from lanmeng_bridge.config import load_settings
from lanmeng_bridge.cron.cron_e import run_cron_e
from lanmeng_bridge.notify.feishu import FeishuNotifier
from lanmeng_bridge.storage.db import get_connection, init_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="cron-e 应急同步 — 一次性拉取吉客云物流公司列表",
        epilog="详见 docs/runbook-cron-ef-failover.md §2",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        required=True,
        help="应急模式 (本脚本唯一模式, 保留为显式标志以防误用)",
    )
    parser.add_argument(
        "--source",
        default="jky",
        help="数据源 (固定 jky, 预留扩展)",
    )
    parser.add_argument(
        "--target",
        default="jky_logistic_cache",
        help="目标表名 (固定 jky_logistic_cache, 预留扩展)",
    )
    return parser.parse_args()


async def _main(args: argparse.Namespace) -> int:
    print(f"[init_logistic] 开始拉取吉客云物流公司列表...")
    print(f"[init_logistic] source={args.source} target={args.target}")

    # 1. 加载 settings + 凭证
    settings = load_settings()
    creds = settings.get("_credentials", {})
    jky_api_key = creds.get("jky_gateway", {}).get("api_key", "")
    if not jky_api_key:
        print(
            "[init_logistic] ERROR: credentials.yaml 缺 jky_gateway.api_key",
            file=sys.stderr,
        )
        return 1

    # 2. 初始化 DB schema (幂等, 不会破坏现有数据)
    init_db()

    # 3. 构造客户端 + 飞书 notifier (即使 webhook 缺失也构造, run_cron_e 会优雅降级)
    jky = create_jky_client(settings)

    feishu_cfg = settings.get("feishu", {})
    feishu_webhook = creds.get("feishu", {}).get("webhook_url", "")
    notifier = FeishuNotifier(
        webhook_url=feishu_webhook,
        p0_at_all=feishu_cfg.get("p0_at_all", True),
    )

    # 4. 抓取 RUN 前的审计表行数, 用于后面计算本次 RUN 写入
    conn = get_connection()
    audit_before = conn.execute(
        "SELECT COUNT(*) FROM jky_logistic_cache_changes"
    ).fetchone()[0]

    # 5. 调用 cron-e 业务逻辑 (重试 + 告警 + diff + 持久化都在内部)
    try:
        await run_cron_e(jky, notifier)
    except Exception as e:
        print(f"[init_logistic] ERROR: run_cron_e 异常: {e}", file=sys.stderr)
        return 1

    # 6. 输出 SOP §2.2 期望格式 (运营对账用)
    try:
        main_count = conn.execute(
            "SELECT COUNT(*) FROM jky_logistic_cache"
        ).fetchone()[0]
        audit_total = conn.execute(
            "SELECT COUNT(*) FROM jky_logistic_cache_changes"
        ).fetchone()[0]
        audit_written = audit_total - audit_before

        # 按变更类型统计本次写入 (取最近 audit_written 条)
        type_rows = conn.execute(
            "SELECT change_type, COUNT(*) AS c FROM ("
            "  SELECT change_type FROM jky_logistic_cache_changes "
            "  ORDER BY change_id DESC LIMIT ?"
            ") GROUP BY change_type",
            (audit_written,),
        ).fetchall()
        diff_stats = {row["change_type"]: row["c"] for row in type_rows}

        print(f"[init_logistic] 共 {main_count} 条记录")
        print(
            f"[init_logistic] diff: 新增 {diff_stats.get('INSERT', 0)} / "
            f"删除 {diff_stats.get('DELETE', 0)} / "
            f"更新 {diff_stats.get('UPDATE', 0)}"
        )
        print(f"[init_logistic] jky_logistic_cache 刷新完成 ({main_count} 条)")
        print(
            f"[init_logistic] jky_logistic_cache_changes 写入 {audit_written} 条审计"
        )
    except Exception as e:
        print(f"[init_logistic] WARN: 统计行数失败: {e}", file=sys.stderr)

    print(f"[init_logistic] 完成")
    return 0


def main() -> int:
    args = parse_args()
    if not args.manual:
        print(
            "[init_logistic] ERROR: 必须传 --manual (应急脚本不应用于日常)",
            file=sys.stderr,
        )
        return 1
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
