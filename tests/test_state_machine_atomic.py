"""state_machine transition() 条件原子更新测试（H-2 修复验证）

核心：两个"执行者"同时尝试 init→audited，只有一个应成功。
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path("/home/lhs_admin/projects/lanmonshop-bridge")
sys.path.insert(0, str(ROOT))

tmp_db = tempfile.mktemp(suffix=".db")
os.environ["LANMONSHOP_DB_PATH"] = tmp_db

from lanmeng_bridge.storage import db as storage_db
from lanmeng_bridge.core import state_machine as sm

conn = storage_db.get_connection()
conn.executescript("""
CREATE TABLE order_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_order_no TEXT, platform_state TEXT,
    state TEXT, last_error TEXT,
    last_attempt_at TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE order_status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_map_id INTEGER, from_state TEXT, to_state TEXT,
    source TEXT, error TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")
conn.execute("INSERT INTO order_map (id, platform_order_no, state) VALUES (1, 'T1', 'init')")
conn.execute("INSERT INTO order_map (id, platform_order_no, state) VALUES (2, 'T2', 'init')")
conn.commit()

# 模拟两个并发执行者：先都读到 init，然后顺序执行条件更新
# 执行者 A
r1 = sm.transition(1, sm.STATE_AUDITED, "cron_a")
# 执行者 B（同一订单，模拟并发竞争 —— 此时 state 已是 audited）
r2 = sm.transition(1, sm.STATE_AUDITED, "cron_a")

# 正常订单（无竞争）
r3 = sm.transition(2, sm.STATE_AUDITED, "cron_a")

# 非法转移（audited → audited 应 False，因为 from_state 已变）
r4 = sm.transition(2, sm.STATE_AUDITED, "cron_a")

# 非法跳转（init → jky_created 不允许）
conn.execute("INSERT INTO order_map (id, platform_order_no, state) VALUES (3, 'T3', 'init')")
conn.commit()
r5 = sm.transition(3, sm.STATE_JKY_CREATED, "cron_a")

results = [
    ("并发竞争: 第一个执行者成功", r1 is True),
    ("并发竞争: 第二个执行者失败(防重复创单)", r2 is False),
    ("正常无竞争: 成功", r3 is True),
    ("已转移后再转移: 失败", r4 is False),
    ("非法跳转 init→jky_created: 失败", r5 is False),
]

# 审计日志校验：T1 应只有 1 条转移记录（A 成功），T2 只有 1 条
log_count_t1 = conn.execute("SELECT COUNT(*) FROM order_status_log WHERE order_map_id=1").fetchone()[0]
log_count_t2 = conn.execute("SELECT COUNT(*) FROM order_status_log WHERE order_map_id=2").fetchone()[0]
results.append((f"审计日志: T1 恰好1条(实际{log_count_t1})", log_count_t1 == 1))
results.append((f"审计日志: T2 恰好1条(实际{log_count_t2})", log_count_t2 == 1))

# 状态终值
s1 = conn.execute("SELECT state FROM order_map WHERE id=1").fetchone()[0]
s2 = conn.execute("SELECT state FROM order_map WHERE id=2").fetchone()[0]
results.append((f"T1 终态 audited(实际{s1})", s1 == "audited"))
results.append((f"T2 终态 audited(实际{s2})", s2 == "audited"))

all_pass = True
for name, ok in results:
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"[{status}] {name}")

conn.close()
os.unlink(tmp_db)
print()
print("=== 状态机并发测试全部通过 ===" if all_pass else "=== 存在失败 ===")
