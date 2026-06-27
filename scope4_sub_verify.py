"""scope4-sub verify — scripts/init_logistic.py + init_sku_mapping.py

verify 矩阵:
A. CLI 接口: --manual 必需, --source/--target 可选
B. 不依赖 APScheduler (脚本独立可执行)
C. cron-e/d 完全停掉时也能跑 (本次 verify 直接调 run_cron_*, 不挂 scheduler)
D. 主表 + 审计表双写
E. stdout 格式与 SOP §2.2 一致
F. 失败路径: 凭证缺失 → exit 1 + stderr
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

# 测试 DB 路径 (verify 完清理)
TEST_DB = "/tmp/lanmenshop-bridge-verify-scope4sub.db"
TEST_CRED = "/tmp/lanmenshop-bridge-verify-credentials.yaml"

# 清理任何残留
for p in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm", TEST_CRED]:
    Path(p).unlink(missing_ok=True)

# 1. 写测试用 credentials.yaml (mock jky_gateway.api_key + feishu.webhook_url)
import yaml
TEST_CRED_DATA = {
    "jky_gateway": {
        "api_key": "test-jky-api-key-for-verify",
        "app_secret": "test-jky-app-secret-for-verify",
    },
    "feishu": {
        "webhook_url": "https://open.feishu.cn/hook/test-disabled",
    },
}
Path(TEST_CRED).write_text(yaml.safe_dump(TEST_CRED_DATA, allow_unicode=True))
os.chmod(TEST_CRED, 0o600)

# 2. 加载测试 credentials → 注入到 process env
ENV = os.environ.copy()
ENV["CREDENTIALS_PATH"] = TEST_CRED
ENV["LANMONSHOP_DB_PATH"] = TEST_DB
ENV["PYTHONPATH"] = "/home/lhs_admin/projects/lanmonshop-bridge"

PROJECT_ROOT = Path("/home/lhs_admin/projects/lanmonshop-bridge")

# ---------- Fixtures ----------
LOGISTIC_FIXTURE_PAGE1 = {
    "code": 0, "msg": "success",
    "data": {"total": 3, "list": [
        {"logisticNo": "SF_EXPRESS", "logisticName": "顺丰速运", "extra": {"id": 1}},
        {"logisticNo": "JD_LOGISTICS", "logisticName": "京东物流", "extra": {"id": 2}},
        {"logisticNo": "EMS", "logisticName": "EMS", "extra": {"id": 3}},
    ]},
}

LOGISTIC_FIXTURE_RUN2 = {
    "code": 0, "msg": "success",
    "data": {"total": 3, "list": [
        {"logisticNo": "SF_EXPRESS", "logisticName": "顺丰速运 (新版名)", "extra": {"id": 1}},
        {"logisticNo": "EMS", "logisticName": "EMS", "extra": {"id": 3}},
        {"logisticNo": "YTO", "logisticName": "圆通速递", "extra": {"id": 4}},
    ]},
}

GOODS_FIXTURE_PAGE1 = {
    "code": 0, "msg": "success",
    "data": {"total": 2, "list": [
        {"goodsNo": "JKY-G001", "goodsName": "可口可乐 330ml", "category": "饮料", "categoryId": "100", "barcode": "6901234567890", "price": 3.5, "stock": 100},
        {"goodsNo": "JKY-G002", "goodsName": "百事可乐 330ml", "category": "饮料", "categoryId": "100", "barcode": "6901234567891", "price": 3.5, "stock": 80},
    ]},
}

GOODS_FIXTURE_RUN2 = {
    "code": 0, "msg": "success",
    "data": {"total": 2, "list": [
        {"goodsNo": "JKY-G001", "goodsName": "可口可乐 330ml", "category": "饮料", "categoryId": "100", "barcode": "6901234567890", "price": 4.0, "stock": 100},
        {"goodsNo": "JKY-G003", "goodsName": "鼠标垫", "category": "周边", "categoryId": "200", "barcode": "6901234567999", "price": 25.0, "stock": 50},
    ]},
}


def run_script(script, *args):
    cmd = [sys.executable, str(PROJECT_ROOT / script), *args]
    return subprocess.run(cmd, env=ENV, capture_output=True, text=True, timeout=30)


def query_db(sql):
    import sqlite3
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    return conn.execute(sql).fetchall()


# ---------- Step 1: CLI 形状 ----------
print("=" * 60)
print("[1] init_logistic.py CLI 形状 (subprocess)")
print("=" * 60)

print("\n[1a] --manual 缺失 → argparse 报错 (exit 2) + 提示 --manual")
result = run_script("scripts/init_logistic.py")
print(f"  exit: {result.returncode}  stderr: {result.stderr.strip()[:120]}")
assert result.returncode == 2, f"argparse 应 exit 2, got {result.returncode}"
assert "--manual" in result.stderr, f"stderr 应提示 --manual"
print("  ✓ PASS")

print("\n[1b] 凭证缺失 → exit 1")
ENV_BAD = ENV.copy()
ENV_BAD["CREDENTIALS_PATH"] = "/tmp/nonexistent-creds-verify-999.yaml"
cmd = [sys.executable, str(PROJECT_ROOT / "scripts/init_logistic.py"), "--manual"]
result = subprocess.run(cmd, env=ENV_BAD, capture_output=True, text=True, timeout=10)
print(f"  exit: {result.returncode}  stderr: {result.stderr.strip()[:200]}")
assert result.returncode == 1
print("  ✓ PASS: 凭证缺失路径走通 (exit 1)")

print("\n[1c] 脚本不依赖 APScheduler (imports only)")
import re
for script in ["scripts/init_logistic.py", "scripts/init_sku_mapping.py"]:
    text = (PROJECT_ROOT / script).read_text()
    # 只查 import 语句, 避免注释误命中
    imports = re.findall(r"^(?:from|import)\s+([\w.]+)", text, re.MULTILINE)
    aps_hits = [m for m in imports if "apscheduler" in m.lower()]
    assert not aps_hits, f"{script} 含 APScheduler import: {aps_hits}"
print("  ✓ PASS: 0 个 APScheduler import 命中")

print("\n[1d] init_sku_mapping.py CLI 形状")
result = run_script("scripts/init_sku_mapping.py")
print(f"  --manual 缺失 exit: {result.returncode}")
assert result.returncode == 2, f"argparse 应 exit 2, got {result.returncode}"
result = subprocess.run(
    [sys.executable, str(PROJECT_ROOT / "scripts/init_sku_mapping.py"), "--manual"],
    env=ENV_BAD, capture_output=True, text=True, timeout=10
)
print(f"  凭证缺失 exit: {result.returncode}")
assert result.returncode == 1, f"凭证缺失应 exit 1, got {result.returncode}"
print("  ✓ PASS: --manual 必需 (argparse exit 2) + 凭证缺失走通 (exit 1)")

# ---------- Step 2: 业务逻辑 in-process (mock httpx) ----------
print()
print("=" * 60)
print("[2] init_logistic.py 业务逻辑 (in-process + mock httpx)")
print("=" * 60)

# 强制 reload
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("lanmeng_bridge"):
        del sys.modules[mod_name]

# 关键: env var 必须在 import db 之前设置 (DB_PATH 是模块级常量)
os.environ["LANMONSHOP_DB_PATH"] = TEST_DB
os.environ["CREDENTIALS_PATH"] = TEST_CRED
os.environ["SETTINGS_PATH"] = str(PROJECT_ROOT / "config" / "settings.yaml")

from lanmeng_bridge.clients.jky import create_jky_client
from lanmeng_bridge.config import load_settings
from lanmeng_bridge.cron.cron_e import run_cron_e
from lanmeng_bridge.notify.feishu import FeishuNotifier
from lanmeng_bridge.storage.db import init_db

init_db()


def make_mock_resp(fixture):
    resp = AsyncMock()
    resp.raise_for_status = lambda: None
    resp.json = lambda: fixture
    resp.status_code = 200
    return resp


async def run_cron_e_with_mock(fixture):
    settings = load_settings()
    jky = create_jky_client(settings)
    jky._client.post = AsyncMock(side_effect=[make_mock_resp(fixture)])
    notifier = FeishuNotifier(
        webhook_url="https://open.feishu.cn/hook/test-disabled",
        p0_at_all=True,
    )
    notifier.alert_p1 = AsyncMock()
    await run_cron_e(jky, notifier)


# RUN1
asyncio.run(run_cron_e_with_mock(LOGISTIC_FIXTURE_PAGE1))

r = query_db("SELECT COUNT(*) AS c FROM jky_logistic_cache")
assert r[0]["c"] == 3, f"主表行数: {r[0]['c']}"
r = query_db("SELECT COUNT(*) AS c FROM jky_logistic_cache_changes")
assert r[0]["c"] == 3, f"审计表行数: {r[0]['c']}"
print(f"\n  RUN1: 主表 3 + 审计表 3 (全 INSERT)  ✓ PASS")

# RUN2
asyncio.run(run_cron_e_with_mock(LOGISTIC_FIXTURE_RUN2))

r = query_db("SELECT COUNT(*) AS c FROM jky_logistic_cache")
assert r[0]["c"] == 3, f"主表行数: {r[0]['c']}"
r = query_db("SELECT COUNT(*) AS c FROM jky_logistic_cache_changes")
assert r[0]["c"] == 6, f"审计表累计: {r[0]['c']}"
rows = query_db("SELECT change_type, COUNT(*) AS c FROM jky_logistic_cache_changes GROUP BY change_type")
types = {row["change_type"] for row in rows}
assert types == {"INSERT", "DELETE", "UPDATE"}, f"变更类型: {types}"
print(f"  RUN2: 主表 3 + 审计表累计 6 (INSERT+DELETE+UPDATE 齐全)  ✓ PASS")

# ---------- Step 3: init_sku_mapping.py 业务逻辑 ----------
print()
print("=" * 60)
print("[3] init_sku_mapping.py 业务逻辑 (in-process + mock httpx)")
print("=" * 60)

# 强制 reload (切到 cron_d)
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("lanmeng_bridge"):
        del sys.modules[mod_name]

from lanmeng_bridge.cron.cron_d import run_cron_d


async def run_cron_d_with_mock(fixture):
    settings = load_settings()
    jky = create_jky_client(settings)
    jky._client.post = AsyncMock(side_effect=[make_mock_resp(fixture)])
    notifier = FeishuNotifier(
        webhook_url="https://open.feishu.cn/hook/test-disabled",
        p0_at_all=True,
    )
    notifier.alert_p1 = AsyncMock()
    await run_cron_d(jky, notifier)


# RUN1
asyncio.run(run_cron_d_with_mock(GOODS_FIXTURE_PAGE1))
r = query_db("SELECT COUNT(*) AS c FROM jky_product_cache")
assert r[0]["c"] == 2
r = query_db("SELECT COUNT(*) AS c FROM jky_product_cache_changes")
assert r[0]["c"] == 2
rows = query_db("SELECT jky_category, COUNT(*) AS c FROM jky_product_cache GROUP BY jky_category")
assert rows[0]["jky_category"] == "饮料"
print(f"\n  RUN1: 主表 2 (饮料) + 审计表 2 INSERT  ✓ PASS")

# RUN2
asyncio.run(run_cron_d_with_mock(GOODS_FIXTURE_RUN2))
r = query_db("SELECT COUNT(*) AS c FROM jky_product_cache")
assert r[0]["c"] == 2, f"主表行数: {r[0]['c']}"
r = query_db("SELECT COUNT(*) AS c FROM jky_product_cache_changes")
# RUN1: 2 INSERT. RUN2: 1 UPDATE (G001 涨价) + 1 DELETE (G002) + 1 INSERT (G003) = 3 changes
# 累计 = 5
assert r[0]["c"] == 5, f"审计表累计: {r[0]['c']} (预期 5)"
rows = query_db("SELECT change_type, COUNT(*) AS c FROM jky_product_cache_changes GROUP BY change_type")
types = {row["change_type"] for row in rows}
assert types == {"INSERT", "DELETE", "UPDATE"}, f"变更类型: {types}"
rows = query_db("SELECT jky_category, COUNT(*) AS c FROM jky_product_cache GROUP BY jky_category")
cats = [row["jky_category"] for row in rows]
# 不依赖排序顺序, 只检查两个分类都在
assert set(cats) == {"饮料", "周边"}, f"category 分布: {cats}"
print(f"  RUN2: 主表 2 (饮料+周边) + 审计表累计 5 (三种变更齐全)  ✓ PASS")

# ---------- 4. End-to-end subprocess (sitecustomize mock httpx) ----------
print()
print("=" * 60)
print("[4] 端到端 subprocess (mock httpx via sitecustomize)")
print("=" * 60)

SITECUSTOMIZE_DIR = "/tmp/sitecustomize_dir"

# prepare fresh DB for end-to-end
for p in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"]:
    Path(p).unlink(missing_ok=True)
Path(TEST_DB).unlink(missing_ok=True)

ENV_E2E = ENV.copy()
ENV_E2E["LANMENSHOP_BRIDGE_TEST_FIXTURE"] = "1"
ENV_E2E["PYTHONPATH"] = SITECUSTOMIZE_DIR + ":" + str(PROJECT_ROOT)
ENV_E2E["SETTINGS_PATH"] = str(PROJECT_ROOT / "config" / "settings.yaml")


def run_e2e(script):
    cmd = [sys.executable, str(PROJECT_ROOT / script), "--manual"]
    return subprocess.run(
        cmd, env=ENV_E2E, capture_output=True, text=True, timeout=30
    )


print("\n[4a] init_logistic.py 端到端")
r = run_e2e("scripts/init_logistic.py")
print(f"  exit: {r.returncode}")
print(f"  stdout:\n{r.stdout}")
assert r.returncode == 0, f"端到端应成功, got {r.returncode}\nstderr: {r.stderr}"
# 验证 SOP §2.2 期望行全部出现
expected_lines = [
    "[init_logistic] 开始拉取吉客云物流公司列表...",
    "[init_logistic] 共 2 条记录",
    "[init_logistic] diff: 新增 2 / 删除 0 / 更新 0",
    "[init_logistic] jky_logistic_cache 刷新完成 (2 条)",
    "[init_logistic] jky_logistic_cache_changes 写入 2 条审计",
    "[init_logistic] 完成",
]
for line in expected_lines:
    assert line in r.stdout, f"stdout 缺: {line}\n实际:\n{r.stdout}"
    print(f"  ✓ stdout 含: {line!r}")

# DB 验证
r = query_db("SELECT COUNT(*) AS c FROM jky_logistic_cache")
assert r[0]["c"] == 2, f"主表: {r[0]['c']}"
r = query_db("SELECT COUNT(*) AS c FROM jky_logistic_cache_changes")
assert r[0]["c"] == 2, f"审计: {r[0]['c']}"
print("  ✓ DB 验证: 主表 2 + 审计 2")

# 清理再跑 init_sku_mapping
for p in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"]:
    Path(p).unlink(missing_ok=True)

print("\n[4b] init_sku_mapping.py 端到端")
r = run_e2e("scripts/init_sku_mapping.py")
print(f"  exit: {r.returncode}")
print(f"  stdout:\n{r.stdout}")
assert r.returncode == 0, f"端到端应成功, got {r.returncode}\nstderr: {r.stderr}"
expected_lines_d = [
    "[init_sku_mapping] 开始拉取吉客云货品列表...",
    "[init_sku_mapping] 共 1 条记录",
    "[init_sku_mapping] diff: 新增 1 / 删除 0 / 更新 0",
    "[init_sku_mapping] jky_product_cache 刷新完成 (1 条)",
    "[init_sku_mapping] category 分布:",
    "[init_sku_mapping] jky_product_cache_changes 写入 1 条审计",
    "[init_sku_mapping] 完成",
]
for line in expected_lines_d:
    assert line in r.stdout, f"stdout 缺: {line}\n实际:\n{r.stdout}"
    print(f"  ✓ stdout 含: {line!r}")

r = query_db("SELECT COUNT(*) AS c FROM jky_product_cache")
assert r[0]["c"] == 1
r = query_db("SELECT COUNT(*) AS c FROM jky_product_cache_changes")
assert r[0]["c"] == 1
print("  ✓ DB 验证: 主表 1 + 审计 1")

# ---------- 5. SOP §2.2 stdout 格式 (代码审计) ----------
print()
print("=" * 60)
print("[4] SOP §2.2 stdout 格式 (代码审计)")
print("=" * 60)

text_e = (PROJECT_ROOT / "scripts/init_logistic.py").read_text()
expected = [
    "[init_logistic] 开始拉取吉客云物流公司列表...",
    "[init_logistic] jky_logistic_cache 刷新完成 (",
    "[init_logistic] jky_logistic_cache_changes 写入 ",
    "[init_logistic] 完成",
]
for line in expected:
    assert line in text_e, f"缺: {line}"
    print(f"  ✓ {line!r}")

text_d = (PROJECT_ROOT / "scripts/init_sku_mapping.py").read_text()
expected_d = [
    "[init_sku_mapping] 开始拉取吉客云货品列表...",
    "[init_sku_mapping] jky_product_cache 刷新完成 (",
    "[init_sku_mapping] category 分布:",
    "[init_sku_mapping] jky_product_cache_changes 写入 ",
    "[init_sku_mapping] 完成",
]
for line in expected_d:
    assert line in text_d, f"缺: {line}"
    print(f"  ✓ {line!r}")

# ---------- Step 5: 清理 ----------
print()
print("=" * 60)
print("[5] 清理 + 最终统计")
print("=" * 60)

# 最终核对一遍测试 DB 内容
r = query_db("SELECT jky_logistic_no, jky_logistic_name FROM jky_logistic_cache ORDER BY jky_logistic_no")
print(f"  jky_logistic_cache 最终: {[(row['jky_logistic_no'], row['jky_logistic_name']) for row in r]}")
r = query_db("SELECT jky_goods_no, jky_category, jky_price FROM jky_product_cache ORDER BY jky_goods_no")
print(f"  jky_product_cache 最终: {[(row['jky_goods_no'], row['jky_category'], row['jky_price']) for row in r]}")

# 清理
for p in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm", TEST_CRED]:
    Path(p).unlink(missing_ok=True)
print(f"  ✓ 临时文件已清理")

print()
print("=" * 60)
print("ALL VERIFY PASSED")
print("=" * 60)
