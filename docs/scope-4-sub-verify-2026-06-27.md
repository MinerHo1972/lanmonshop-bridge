# scope 4 子项 verify — scripts/init_logistic.py + init_sku_mapping.py

| 项 | 值 |
|---|---|
| 任务 id | t_e522da71 |
| 父任务 | scope 4 (t_1d4b92e6, cron 业务逻辑已 done) + scope 6 (t_21cc4ab4, SOP 文档) |
| 实施人 | codewhale |
| 日期 | 2026-06-27 |
| 状态 | PASSED (全 4 verify 项绿) |

## 背景

scope 6 (testing) 已完成 SOP 文档化 (`docs/runbook-cron-ef-failover.md` §2.2 / `docs/runbook-mark-closed.md` §5.1), 但 scope 4 主任务体 (t_1d4b92e6) **未明确包含**这两个应急脚本的实施。本任务作为 scope 4 子项, 由 codewhale 补齐:

- `scripts/init_logistic.py` — cron-e 失败时人工一次性同步脚本
- `scripts/init_sku_mapping.py` — cron-d 失败时人工一次性同步脚本 (v0.2 bootstrap 改造)

复用策略 (减法原则): **不重写** soft-delete + diff-INSERT 算法, 直接调 `cron_e.run_cron_e` / `cron_d.run_cron_d`, 仅在脚本层加 CLI + SOP-style stdout 输出。

## 实施产出

| 文件 | 行数 | 字节 | 权限 | 说明 |
|---|---|---|---|---|
| `scripts/init_logistic.py` | 154 | 5403 | 755 | cron-e 应急同步 |
| `scripts/init_sku_mapping.py` | 167 | 5919 | 755 | cron-d 应急同步 |
| `scope4_sub_verify.py` | 449 | 13.5K | 644 | in-process + e2e verify |

### 设计要点

1. **CLI 形状**:
   - `--manual` **必需** (argparse exit 2 缺参) — 防误用为日常脚本
   - `--source jky` 默认 (预留扩展)
   - `--target jky_logistic_cache` 默认 (仅 logistic 脚本)

2. **凭证**: 从 `~/.hermes/data/credentials.yaml` 读 `_credentials.jky_gateway.api_key`, 缺失 → exit 1 + stderr

3. **不依赖 APScheduler**: 0 个 apscheduler import 命中, 脚本独立可执行, 不与 cron 抢 SQLite 写锁

4. **不注册 systemd**: 应急脚本按需触发, 避免长期顶班

5. **stdout 格式严格匹配 SOP §2.2**:
   ```
   [init_logistic] 开始拉取吉客云物流公司列表...
   [init_logistic] source=jky target=jky_logistic_cache
   [init_logistic] 共 N 条记录
   [init_logistic] diff: 新增 X / 删除 Y / 更新 Z
   [init_logistic] jky_logistic_cache 刷新完成 (N 条)
   [init_logistic] jky_logistic_cache_changes 写入 W 条审计
   [init_logistic] 完成
   ```
   (init_sku_mapping.py 同样 7 行 + category 分布行)

6. **diff 统计实现**: 用 `audit_before` / `audit_total` 算"本次 RUN 新增", 取最近 N 条按 change_type 分组, 确保不混淆历史累计

## verify 矩阵 (4 项全 PASS)

### 1. CLI 形状 (subprocess, 4 子项)

| 子项 | 输入 | 预期 | 实际 | 状态 |
|---|---|---|---|---|
| 1a | `init_logistic.py` (无 --manual) | exit 2 + stderr 提示 --manual | exit 2 + argparse usage | OK |
| 1b | `init_logistic.py --manual` + 凭证缺失 | exit 1 + stderr ERROR | exit 1 + 凭证错误提示 | OK |
| 1c | grep `apscheduler` (imports only) | 0 命中 | 0 命中 (regex 仅匹配 `^(?:from\|import)\s+`) | OK |
| 1d | init_sku_mapping.py 同 1a+1b | 同上 | 同上 | OK |

### 2. init_logistic.py 业务逻辑 (in-process mock httpx)

| RUN | 主表 | 审计表 | 变更类型 | 状态 |
|---|---|---|---|---|
| RUN1 (SF/JD/EMS) | 3 | 3 | 全 INSERT | OK |
| RUN2 (SF改名/JD删/YTO增) | 3 (SF新名/EMS/YTO) | 6 累计 | INSERT+DELETE+UPDATE 齐全 | OK |

### 3. init_sku_mapping.py 业务逻辑 (in-process mock httpx)

| RUN | 主表 | 审计表 | category | 变更类型 | 状态 |
|---|---|---|---|---|---|
| RUN1 (G001+G002 饮料) | 2 | 2 | 饮料=2 | 全 INSERT | OK |
| RUN2 (G001涨价/G002删/G003周边) | 2 | 5 累计 | 饮料+周边 | INSERT+DELETE+UPDATE 齐全 | OK |

### 4. 端到端 subprocess (sitecustomize mock httpx)

完整跑 `--manual` 路径, 验证:
- OK init_logistic.py: exit 0, stdout 7 行全部命中 SOP §2.2 (含 `共 2 条记录` + `diff: 新增 2 / 删除 0 / 更新 0`)
- OK init_logistic.py: DB 主表 2 + 审计表 2
- OK init_sku_mapping.py: exit 0, stdout 7 行 + category 分布行全部命中
- OK init_sku_mapping.py: DB 主表 1 + 审计表 1

实际 stdout:
```
[init_logistic] 开始拉取吉客云物流公司列表...
[init_logistic] source=jky target=jky_logistic_cache
[init_logistic] 共 2 条记录
[init_logistic] diff: 新增 2 / 删除 0 / 更新 0
[init_logistic] jky_logistic_cache 刷新完成 (2 条)
[init_logistic] jky_logistic_cache_changes 写入 2 条审计
[init_logistic] 完成
```

## run 命令

```bash
# 跑全部 verify (1-5)
cd /home/lhs_admin/projects/lanmenshop-bridge
python3 scope4_sub_verify.py

# 单跑 CLI 形状 (不需要 DB/凭证)
python3 scripts/init_logistic.py  # 应 exit 2 (缺 --manual)
python3 scripts/init_logistic.py --manual  # 应 exit 1 (凭证缺失)
python3 scripts/init_sku_mapping.py  # 应 exit 2
python3 scripts/init_sku_mapping.py --manual  # 应 exit 1

# 真实 ECS 上跑 (凭证就绪后)
ssh root@8.153.195.8
cd /home/lhs_admin/projects/lanmenshop-bridge
python3 scripts/init_logistic.py --manual --source jky --target jky_logistic_cache
python3 scripts/init_sku_mapping.py --manual --source jky
```

## 已知前置依赖 (runbook §2.5)

按 SOP §2.5, 跑应急脚本前必须:
1. 确认 `lanmeng-bridge.service` 未跑 (防 SQLite 写锁竞争): `sudo systemctl is-active lanmeng-bridge`
2. 若 service 在跑, 等 5min 让 cron 自然结束再跑
3. 仍失败 → 升级 P0 服务可用性事件

## 已知上游 gap (待 scope 5 处理)

1. **配置路径 bug** (`lanmeng_bridge/config.py`):
   `SETTINGS_PATH = Path(__file__).parent.parent.parent / "config" / "settings.yaml"`
   在 WSL 项目根 (`/home/lhs_admin/projects/lanmenshop-bridge/lanmeng_bridge/config.py`) 解析为 `/home/lhs_admin/projects/config/settings.yaml` (不存在)
   本地 verify 走 `SETTINGS_PATH` env var 绕过; ECS 实际部署在 `/opt/lanmonshop-bridge/` 时路径不同, 该路径解析具体如何待 scope 5 verify (本任务不动)
2. **`_credentials.jky_gateway` + `_credentials.feishu` 字段缺**: credentials.yaml 当前只有 `jky` + `lanmenshop` + `cloudflare` 等顶层 section, `jky_gateway.api_key` 和 `feishu.webhook_url` 字段不存在
   ECS 实际能跑通意味着 production credentials.yaml 已含这些字段 (未同步到本仓库)
   scope 5 验证时需要 cross-check 凭证是否到位

## 不在 scope (显式不做)

- 不注册 systemd unit (应急脚本按需)
- 不加 cron 调度 (应急 ≠ cron 替代)
- 不改 cron_e.py / cron_d.py (代码复用优先于重写)
- 不引入新依赖 (脚本仅用 stdlib + 现有 lanmeng_bridge)
