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
from typing import Dict, Optional

DB_PATH = os.environ.get(
    "LANMONSHOP_DB_PATH",
    str(Path.home() / ".hermes" / "data" / "lanmonshop-bridge.db"),
)

# ---------- Schema ----------

SCHEMA_SQL = """
-- 订单映射（主表）
CREATE TABLE IF NOT EXISTS order_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_order_no TEXT NOT NULL,           -- 中台 orderNo（渠道单号）
    platform_order_id INTEGER,                 -- 中台 orderId（发回传用）
    platform_state INTEGER,                    -- 中台原始 state（cron-c 对账 key）
    jky_trade_no TEXT,                         -- 吉客云销售单号
    logistic_no TEXT,                          -- 物流单号
    state TEXT NOT NULL DEFAULT 'init',        -- 状态机当前态
    retry_count INTEGER DEFAULT 0,             -- 发货回传失败重试计数
    next_retry_at TIMESTAMP,                   -- 下次重试时间
    last_error TEXT,                           -- 最近一次错误
    last_attempt_at TIMESTAMP,                 -- 最近一次状态变更时间
    closed_at TIMESTAMP,                       -- 异常关闭时间
    closed_by TEXT,                            -- 异常关闭人（运营姓名）
    closed_note TEXT,                          -- 异常关闭说明
    order_items_json TEXT,                     -- 原始 orderItemId
    jky_state TEXT,
    platform_unified TEXT,
    jky_unified TEXT,
    jky_effective_unified TEXT DEFAULT '',
    bridge_unified TEXT,
    related_order_nos TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_order_map_platform_jky_uniq
  ON order_map(platform_order_no, jky_trade_no);
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

-- api 调用日志（两侧，长期保存）
CREATE TABLE IF NOT EXISTS api_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    method TEXT NOT NULL,
    request_body TEXT,
    response_body TEXT,
    http_status INTEGER,
    api_code INTEGER,
    api_sub_code TEXT,
    error TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_api_log_source ON api_call_log(source);
CREATE INDEX IF NOT EXISTS idx_api_log_method ON api_call_log(method);
CREATE INDEX IF NOT EXISTS idx_api_log_created ON api_call_log(created_at);
CREATE INDEX IF NOT EXISTS idx_api_call_log_source_method
  ON api_call_log(source, method, created_at);

-- cron 游标（增量拉取位置）
CREATE TABLE IF NOT EXISTS cron_cursor (
    cursor_key TEXT PRIMARY KEY,
    cursor_value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 🆕 合并/拆分事件追踪
CREATE TABLE IF NOT EXISTS order_merge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_trade_no TEXT NOT NULL,
    source_online_trade_no TEXT,
    merge_type TEXT NOT NULL,            -- 'merge' / 'split'
    target_trade_no TEXT,
    target_online_trade_no TEXT,
    jky_status INTEGER,
    order_map_id INTEGER,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_map_id) REFERENCES order_map(id)
);
CREATE INDEX IF NOT EXISTS idx_order_merge_source ON order_merge(source_trade_no);
CREATE INDEX IF NOT EXISTS idx_order_merge_target ON order_merge(target_trade_no);

-- 物流同步任务
CREATE TABLE IF NOT EXISTS express_sync (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform_order_no TEXT NOT NULL,
  jky_trade_no TEXT,
  express_no TEXT NOT NULL,
  express_code TEXT,
  sync_status TEXT DEFAULT 'pending',
  fault_detail TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_express_sync_uniq
  ON express_sync(platform_order_no, jky_trade_no, express_no);

CREATE TABLE IF NOT EXISTS express_sync_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  express_sync_id INTEGER NOT NULL REFERENCES express_sync(id),
  order_item_id INTEGER NOT NULL,
  product_no TEXT,
  qty INTEGER NOT NULL,
  UNIQUE(express_sync_id, order_item_id)
);

-- 🆕 Admin session 持久化（重启不丢登录态）
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_open_id TEXT NOT NULL,
    user_union_id TEXT DEFAULT '',
    user_name TEXT DEFAULT '',
    user_avatar TEXT DEFAULT '',
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- 🆕 三方对账报告（cron-f 每日生成）
CREATE TABLE IF NOT EXISTS reconciliation_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,           -- YYYY-MM-DD
    run_id TEXT NOT NULL,                -- 唯一 run_id
    summary_json TEXT NOT NULL,          -- 统计数据汇总
    deviations_json TEXT,                -- JSON 差异列表
    daily_trend_json TEXT,               -- 每日趋势
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_recon_report_date ON reconciliation_report(report_date);
"""


# ---------- 增量迁移（幂等）----------


_MIGRATIONS = [
    # v0.3.6 / P9: jky_product_cache 加 jky_category 字段（已有库 ALTER 加列, 新库走 CREATE TABLE）
    "ALTER TABLE jky_product_cache ADD COLUMN jky_category TEXT",
    # v0.3.6 / bugfix #5: order_map 加 order_items_json（物流回传需要原始 orderItemId）
    "ALTER TABLE order_map ADD COLUMN order_items_json TEXT",
    # 三端对账: 统一态 4 列
    "ALTER TABLE order_map ADD COLUMN jky_state TEXT",
    "ALTER TABLE order_map ADD COLUMN platform_unified TEXT",
    "ALTER TABLE order_map ADD COLUMN jky_unified TEXT",
    "ALTER TABLE order_map ADD COLUMN jky_effective_unified TEXT DEFAULT ''",
    "ALTER TABLE order_map ADD COLUMN bridge_unified TEXT",
    "ALTER TABLE order_map ADD COLUMN related_order_nos TEXT DEFAULT ''",
    # P1: 发货回传重试字段
    "ALTER TABLE order_map ADD COLUMN retry_count INTEGER DEFAULT 0",
    "ALTER TABLE order_map ADD COLUMN next_retry_at TIMESTAMP",
    "ALTER TABLE order_map ADD COLUMN last_error TEXT",
    # v0.3.6 / merge追踪: order_merge 补充缺失列（已有库无此列, 新库已有）
    "ALTER TABLE order_merge ADD COLUMN source_online_trade_no TEXT",
    "ALTER TABLE order_merge ADD COLUMN target_trade_no TEXT",
    "ALTER TABLE order_merge ADD COLUMN target_online_trade_no TEXT",
    "ALTER TABLE order_merge ADD COLUMN jky_status INTEGER",
    "ALTER TABLE order_merge ADD COLUMN order_map_id INTEGER",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """幂等应用增量迁移。

    兼容两种 DB 状态:
    - 旧库（v0.3.5 之前）: jky_product_cache 已存在但缺 jky_category → ALTER 成功
    - 新库（v0.3.6 全新）: jky_product_cache 不存在 → ALTER 失败 "no such table" → 跳过
      （CREATE TABLE 会在 executescript(SCHEMA_SQL) 时带 jky_category 直接建）

    错误处理:
    - "duplicate column name" / "already exists" → 已迁移过, 跳过
    - "no such table" → 全新库, 由 SCHEMA_SQL 建表时带新列, 跳过
    - 其他 → raise
    """
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if (
                "duplicate column" in msg
                or "already exists" in msg
                or "no such table" in msg  # 新库: 表还没建, CREATE TABLE 会带新列
            ):
                continue
            raise
    conn.commit()


def _order_map_has_platform_only_unique(conn: sqlite3.Connection) -> bool:
    """检测旧版 order_map 是否仍有 UNIQUE(platform_order_no)。"""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='order_map'"
    ).fetchone()
    if not table:
        return False

    for idx in conn.execute("PRAGMA index_list(order_map)").fetchall():
        idx_name = idx["name"] if isinstance(idx, sqlite3.Row) else idx[1]
        is_unique = idx["unique"] if isinstance(idx, sqlite3.Row) else idx[2]
        if not is_unique:
            continue
        cols = [
            r["name"] if isinstance(r, sqlite3.Row) else r[2]
            for r in conn.execute(f"PRAGMA index_info({idx_name})").fetchall()
        ]
        if cols == ["platform_order_no"]:
            return True
    return False


def _migrate_order_map_unique_key(conn: sqlite3.Connection) -> None:
    """将旧 UNIQUE(platform_order_no) 迁移为组合唯一索引。"""
    if not _order_map_has_platform_only_unique(conn):
        return

    existing_cols = {
        r["name"] if isinstance(r, sqlite3.Row) else r[1]
        for r in conn.execute("PRAGMA table_info(order_map)").fetchall()
    }
    target_cols = [
        "id", "platform_order_no", "platform_order_id", "platform_state",
        "jky_trade_no", "logistic_no", "state", "retry_count",
        "next_retry_at", "last_error", "last_attempt_at", "closed_at",
        "closed_by", "closed_note", "order_items_json", "jky_state",
        "platform_unified", "jky_unified", "jky_effective_unified",
        "bridge_unified", "related_order_nos", "created_at", "updated_at",
    ]
    copy_cols = [c for c in target_cols if c in existing_cols]
    col_sql = ", ".join(copy_cols)

    fk_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.commit()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute(
            """CREATE TABLE order_map__p1_migrate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_order_no TEXT NOT NULL,
                platform_order_id INTEGER,
                platform_state INTEGER,
                jky_trade_no TEXT,
                logistic_no TEXT,
                state TEXT NOT NULL DEFAULT 'init',
                retry_count INTEGER DEFAULT 0,
                next_retry_at TIMESTAMP,
                last_error TEXT,
                last_attempt_at TIMESTAMP,
                closed_at TIMESTAMP,
                closed_by TEXT,
                closed_note TEXT,
                order_items_json TEXT,
                jky_state TEXT,
                platform_unified TEXT,
                jky_unified TEXT,
                jky_effective_unified TEXT DEFAULT '',
                bridge_unified TEXT,
                related_order_nos TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            f"INSERT INTO order_map__p1_migrate ({col_sql}) "
            f"SELECT {col_sql} FROM order_map"
        )
        conn.execute("DROP TABLE order_map")
        conn.execute("ALTER TABLE order_map__p1_migrate RENAME TO order_map")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if fk_enabled else 'OFF'}")


# ---------- Connection Pool ----------
_connections: Dict[str, "sqlite3.Connection"] = {}


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
    # 先跑 SCHEMA_SQL 确保表存在（IF NOT EXISTS 幂等），再迁移列和索引
    conn.executescript(SCHEMA_SQL)
    _apply_migrations(conn)
    _migrate_order_map_unique_key(conn)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def close_all():
    """关闭所有连接（用于优雅退出）"""
    for path, conn in _connections.items():
        conn.close()
    _connections.clear()


def log_api_call(
    source: str,
    method: str,
    request_body: str = "",
    response_body: str = "",
    http_status: int = 0,
    api_code: int = 0,
    api_sub_code: str = "",
    error: str = "",
    duration_ms: int = 0,
) -> None:
    """写入 API 调用日志到 api_call_log 表"""
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO api_call_log
               (source, method, request_body, response_body,
                http_status, api_code, api_sub_code, error, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, method, request_body, response_body,
             http_status, api_code, api_sub_code, error, duration_ms),
        )
        conn.commit()
    except Exception:
        _db_logger = logging.getLogger("lanmonshop-bridge.db")
        _db_logger.exception("[db] log_api_call 写入失败")


def get_cursor(cursor_key: str, default: str = "") -> str:
    """读取 cron 游标值"""
    conn = get_connection()
    row = conn.execute(
        "SELECT cursor_value FROM cron_cursor WHERE cursor_key = ?",
        (cursor_key,),
    ).fetchone()
    return row["cursor_value"] if row else default


def set_cursor(cursor_key: str, cursor_value: str) -> None:
    """写入 cron 游标值（UPSERT）"""
    conn = get_connection()
    conn.execute(
        """INSERT INTO cron_cursor (cursor_key, cursor_value, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(cursor_key) DO UPDATE SET
               cursor_value = excluded.cursor_value,
               updated_at = CURRENT_TIMESTAMP""",
        (cursor_key, cursor_value),
    )
    conn.commit()
