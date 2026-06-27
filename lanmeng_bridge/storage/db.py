"""SQLite 持久化 — 8 张表 schema + 连接管理

Scope 2 扩展 (v0.3.6):
- 新增 jky_logistic_cache (cron-e 维护)
- 新增 jky_product_cache_changes + jky_logistic_cache_changes (审计即架构, P1 修正)
- 新增 alert_counter (P2→P1 升级滑动窗口)
- jky_product_cache 加 jky_category 字段 (P9: 饮料/周边分类)
- WAL mode + busy_timeout=5000 已就位 (防止 cron-a/c/d/e 写锁碰撞)
"""

import sqlite3
import os
from pathlib import Path
from typing import Optional

DB_PATH = os.environ.get(
    "LANMONSHOP_DB_PATH",
    str(Path.home() / ".hermes" / "data" / "lanmonshop-bridge.db"),
)

# ---------- Schema ----------

SCHEMA_SQL = """
-- 订单映射（主表）
CREATE TABLE IF NOT EXISTS order_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_order_no TEXT UNIQUE NOT NULL,    -- 中台 orderNo（渠道单号）
    platform_order_id INTEGER,                 -- 中台 orderId（发回传用）
    platform_state INTEGER,                    -- 中台原始 state（cron-c 对账 key）
    jky_trade_no TEXT,                         -- 吉客云销售单号
    logistic_no TEXT,                          -- 物流单号
    state TEXT NOT NULL DEFAULT 'init',        -- 状态机当前态
    retry_count INTEGER DEFAULT 0,             -- 发货回传失败重试计数
    last_error TEXT,                           -- 最近一次错误
    last_attempt_at TIMESTAMP,                 -- 最近一次状态变更时间
    closed_at TIMESTAMP,                       -- 异常关闭时间
    closed_by TEXT,                            -- 异常关闭人（运营姓名）
    closed_note TEXT,                          -- 异常关闭说明
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_order_map_state ON order_map(state);
CREATE INDEX IF NOT EXISTS idx_order_map_updated ON order_map(updated_at);
CREATE INDEX IF NOT EXISTS idx_order_map_platform_state ON order_map(platform_state);

-- 状态变更审计
CREATE TABLE IF NOT EXISTS order_status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_map_id INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    source TEXT NOT NULL,
    error TEXT,
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_map_id) REFERENCES order_map(id)
);
CREATE INDEX IF NOT EXISTS idx_status_log_order ON order_status_log(order_map_id);
CREATE INDEX IF NOT EXISTS idx_status_log_ts ON order_status_log(ts);

-- SKU 映射
CREATE TABLE IF NOT EXISTS sku_mapping (
    platform_sku_no TEXT PRIMARY KEY,
    platform_barcode TEXT,
    jky_goods_no TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sku_barcode ON sku_mapping(platform_barcode);

-- 吉客云货品列表 cache（cron-d 每日刷新）
CREATE TABLE IF NOT EXISTS jky_product_cache (
    jky_goods_no TEXT PRIMARY KEY,
    jky_goods_name TEXT,
    jky_barcode TEXT,
    jky_category TEXT,                  -- 🆕 P9: 吉客云分类名（"饮料"/"周边"），运营审计可见
    jky_category_id TEXT,               -- 吉客云分类 ID（备用, scope 4 可选启用）
    jky_price REAL,                     -- 吉客云售价（一期不入 ordercreate, 保留）
    jky_stock INTEGER,                  -- 吉客云库存（一期不入 ordercreate, 保留）
    raw_json TEXT,                      -- 原始 API 响应（审计即架构）
    fetched_at TIMESTAMP,               -- 拉取时间（cron-d 监控用）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jky_product_barcode ON jky_product_cache(jky_barcode);
CREATE INDEX IF NOT EXISTS idx_jky_product_category ON jky_product_cache(jky_category);  -- 🆕 P9: 审计/筛选
CREATE INDEX IF NOT EXISTS idx_jky_product_fetched ON jky_product_cache(fetched_at);

-- 🆕 P5: 吉客云物流公司 cache（cron-e 每日刷新）
CREATE TABLE IF NOT EXISTS jky_logistic_cache (
    jky_logistic_no TEXT PRIMARY KEY,   -- 吉客云物流编码（如 "SF_EXPRESS"）
    jky_logistic_name TEXT,             -- 吉客云物流名（如 "顺丰速运"）
    raw_json TEXT,                      -- 原始 API 响应（审计即架构）
    fetched_at TIMESTAMP,               -- 拉取时间（cron-e 监控用）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jky_logistic_fetched ON jky_logistic_cache(fetched_at);

-- 🆕 P1 审计修正: cron-d 货品 cache 变更历史表
CREATE TABLE IF NOT EXISTS jky_product_cache_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    jky_goods_no TEXT NOT NULL,
    change_type TEXT,                   -- 'INSERT' / 'DELETE' / 'UPDATE'
    old_value TEXT,                     -- JSON（变更前快照, DELETE 时为当前值）
    new_value TEXT,                     -- JSON（变更后快照, DELETE 时为 NULL）
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cron_run_id TEXT                    -- 关联 cron-d run_id（用于追溯第几次拉取）
);
CREATE INDEX IF NOT EXISTS idx_jpc_changes_goods ON jky_product_cache_changes(jky_goods_no);
CREATE INDEX IF NOT EXISTS idx_jpc_changes_time ON jky_product_cache_changes(changed_at);

-- 🆕 P1 审计修正: cron-e 物流 cache 变更历史表
CREATE TABLE IF NOT EXISTS jky_logistic_cache_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    jky_logistic_no TEXT NOT NULL,
    change_type TEXT,                   -- 'INSERT' / 'DELETE' / 'UPDATE'
    old_value TEXT,
    new_value TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cron_run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_jlc_changes_logistic ON jky_logistic_cache_changes(jky_logistic_no);
CREATE INDEX IF NOT EXISTS idx_jlc_changes_time ON jky_logistic_cache_changes(changed_at);

-- 🆕 P2 升级修正: P2→P1 滑动窗口聚合表（按 exception class 聚合）
CREATE TABLE IF NOT EXISTS alert_counter (
    exception_class TEXT PRIMARY KEY,   -- e.g. 'JKYRateLimitError' / 'JkyOrderCancelRejectedError'
    window_start_ts INTEGER NOT NULL,   -- 当前 30min 滑动窗口起点
    count INTEGER NOT NULL DEFAULT 0,   -- 当前窗口内触发次数
    last_error TEXT,                    -- 最近一次错误信息（飞书告警附）
    upgraded_to_p1_at TIMESTAMP         -- 升级 P1 时间（NULL = 未升级; 升级后 1h 内不再降回 P2）
);
CREATE INDEX IF NOT EXISTS idx_alert_counter_window ON alert_counter(window_start_ts);
"""


# ---------- 增量迁移（幂等）----------


_MIGRATIONS = [
    # v0.3.6 / P9: jky_product_cache 加 jky_category 字段（已有库 ALTER 加列, 新库走 CREATE TABLE）
    "ALTER TABLE jky_product_cache ADD COLUMN jky_category TEXT",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """幂等应用增量迁移 (CREATE TABLE 不会重复建, ADD COLUMN 已存在列会失败被吞)."""
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            # "duplicate column name" = 已迁移过, 跳过
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
    conn.commit()


# ---------- Connection Pool ----------
_connections: dict[str, sqlite3.Connection] = {}


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """获取或创建 SQLite 连接（单例 per path）"""
    path = db_path or DB_PATH
    if path not in _connections:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _connections[path] = conn
    return _connections[path]


def init_db(db_path: Optional[str] = None):
    """初始化数据库 schema（幂等）

    顺序:
    1. ALTER TABLE 增量迁移（先加列, 兼容旧 DB 缺 jky_category 情况）
    2. CREATE TABLE / CREATE INDEX（IF NOT EXISTS 幂等）
    """
    conn = get_connection(db_path)
    # 先迁移列, 让后续 SCHEMA_SQL 的 CREATE INDEX 能找到目标列
    _apply_migrations(conn)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def close_all():
    """关闭所有连接（用于优雅退出）"""
    for path, conn in _connections.items():
        conn.close()
    _connections.clear()
