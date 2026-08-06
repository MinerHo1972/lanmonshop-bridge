# 状态机全链路审计报告 — state_machine.py

> 日期: 2026-07-08
> 审计范围: `lanmeng_bridge/core/state_machine.py` + 全部 6 个调用方
> 触发: cron-a 手动执行发现订单卡死（AUDITED→SKIPPED 转移缺失）
> 原则: 每处缺失 = 生产隐患，不放过任何隐性路径

---

## 1. 当前状态定义

| 状态 | 含义 | 终态 |
|------|------|------|
| `init` | cron-a 刚发现 | 否 |
| `audited` | 中台已自动过审 | 否 |
| `jky_created` | 吉客云销售单已创建 | 否 |
| `jky_shipped` | 吉客云已发货 | 否 |
| `synced` | 物流已回传中台 | 否 |
| `done` | 闭环 | **是** |
| `failed` | 回传/创单失败 | 否 |
| `skipped` | SKU 缺映射跳过 | 否 |
| `jky_cancelled` | 中台退→吉客云取消 | **是** |
| `cancelled` | 中台 -2/-3/-4 | **是** |

---

## 2. 全部调用路径与允许转移对比表

每行 = 代码中一条 `st_transition()` / `transition()` 调用。✅=允许，❌=静默失败（bug）。

### 2.1 cron_a

| 代码位置 | 调用 | 当前状态 | 目标状态 | 结果 |
|----------|------|----------|----------|------|
| cron_a.py:133 | st_transition(map_id, STATE_CANCELLED) | init | cancelled | ✅ |
| cron_a.py:144 | st_transition(map_id, STATE_AUDITED) | init | audited | ✅ |
| cron_a.py:148 | st_transition(map_id, STATE_FAILED) | init | failed | ✅ |
| **cron_a.py:161** | **st_transition(map_id, STATE_SKIPPED)** | **audited**（← 144已转移） | **skipped** | ❌ **bug → 已修** |
| **cron_a.py:172** | **st_transition(map_id, STATE_SKIPPED)** | **audited** | **skipped** | ❌ **bug → 已修** |
| **cron_a.py:188** | **st_transition(map_id, STATE_SKIPPED)** | **audited** | **skipped** | ❌ **bug → 已修** |
| **cron_a.py:195** | **st_transition(map_id, STATE_SKIPPED)** | **audited** | **skipped** | ❌ **bug → 已修** |
| cron_a.py:148 | st_transition(map_id, STATE_FAILED) | audited | failed | ✅ |
| cron_a.py:276 | st_transition(map_id, STATE_JKY_CREATED) | audited | jky_created | ✅ |
| cron_a.py:242 | st_transition(map_id, STATE_FAILED) | — | failed | ✅ |
| cron_a.py:259 | st_transition(map_id, STATE_FAILED) | — | failed | ✅ |
| cron_a.py:284 | st_transition(map_id, STATE_FAILED) | — | failed | ✅ |

### 2.2 cron_b

| 代码位置 | 调用 | 当前状态 | 目标状态 | 结果 |
|----------|------|----------|----------|------|
| cron_b.py:163 | st_transition(map_id, STATE_JKY_SHIPPED) | jky_created | jky_shipped | ✅ |
| cron_b.py:170 | st_transition(map_id, STATE_FAILED) | jky_shipped | failed | ✅ |
| cron_b.py:198 | st_transition(map_id, STATE_FAILED) | — | failed | ✅ |
| cron_b.py:237 | st_transition(map_id, STATE_SYNCED) | jky_shipped | synced | ✅ |
| cron_b.py:238 | st_transition(map_id, STATE_DONE) | synced | done | ✅ |
| cron_b.py:286 | st_transition(map_id, STATE_FAILED) | — | failed | ✅ |

### 2.3 cron_c

| 代码位置 | 调用 | 可能起始状态 | 目标状态 | 结果 |
|----------|------|-------------|----------|------|
| cron_c.py:162 | st_transition(id, STATE_JKY_CANCELLED) | **audited** | jky_cancelled | ❌ **待确认** |
| cron_c.py:162 | st_transition(id, STATE_JKY_CANCELLED) | **jky_created** | jky_cancelled | ✅ |
| cron_c.py:162 | st_transition(id, STATE_JKY_CANCELLED) | **jky_shipped** | jky_cancelled | ❌ **待确认** |
| cron_c.py:162 | st_transition(id, STATE_JKY_CANCELLED) | **synced** | jky_cancelled | ❌ **待确认** |
| cron_c.py:162 | st_transition(id, STATE_JKY_CANCELLED) | **failed** | jky_cancelled | ❌ **待确认** |
| cron_c.py:162 | st_transition(id, STATE_JKY_CANCELLED) | **skipped** | jky_cancelled | ❌ **潜在** |

> 注: cron_c 查询 `updated_at >= cutoff` 的全部订单，按蓝盟状态过滤。只要蓝盟返回已取消且订单有 jky_trade_no，就会尝试 JKY 取消。各异常态均可能命中。

### 2.4 app.py (JKY webhook)

| 代码位置 | 调用 | 条件 | 目标状态 | 结果 |
|----------|------|------|----------|------|
| app.py:296 | transition(map_id, STATE_JKY_SHIPPED) | **current_state == "jky_created"** | jky_shipped | ✅ |
| **app.py:296** | **transition(map_id, STATE_JKY_SHIPPED)** | **current_state == "failed"** | **jky_shipped** | ❌ **bug** |
| **app.py:296** | **transition(map_id, STATE_JKY_SHIPPED)** | **current_state == "audited"** | **jky_shipped** | ❌ **bug** |

> 注: webhook 显式写了 `if current_state in ("jky_created", "failed", "audited")`，后两个状态在 state_machine 中无对应允许转移。

### 2.5 admin.py (人工重试)

| 代码位置 | 调用 | 隐含起始 | 目标状态 | 结果 |
|----------|------|----------|----------|------|
| admin.py:1182 | transition(order_id, STATE_JKY_CANCELLED) | jky_created / failed | jky_cancelled | ✅/❌ |
| admin.py:1408 | transition(order_id, STATE_AUDITED) | failed | audited | ✅ |
| admin.py:1520 | transition(order_id, STATE_JKY_CREATED) | failed | jky_created | ✅ |
| admin.py:1211/1357/1517 | transition(order_id, STATE_FAILED) | — | failed | ✅ |

---

## 3. 发现的 Bug 清单

### P0 — 订单永久卡死（已修复）

**命名**: `AUDITED→SKIPPED` 缺失
**文件**: `state_machine.py:27`
**原值**: `STATE_AUDITED: [STATE_JKY_CREATED, STATE_FAILED]`
**修复**: `STATE_AUDITED: [STATE_JKY_CREATED, STATE_SKIPPED, STATE_FAILED]`
**影响**: cron-a SKU 映射失败后状态无法前进，订单静默卡在 `audited` 终态之前

### P1 — Webhook 发货更新被静默拦截

**命名**: `FAILED→JKY_SHIPPED` + `AUDITED→JKY_SHIPPED` 缺失
**文件**: `app.py:296` + `state_machine.py:27`
**场景**: JKY 发货后 webhook 回调 → `transition(map_id, STATE_JKY_SHIPPED, "webhook")`
        但 `state_machine.py` 不允许从 `failed` 或 `audited` 跳到 `jky_shipped`
**影响**: 即使物流已发出，状态机拒绝前进。cron-b 回传逻辑依赖 `jky_shipped` 状态，断裂后 syncOrderExpress 不会触发
**修复**: 加 `STATE_AUDITED: [..., STATE_JKY_SHIPPED]` 和 `STATE_FAILED: [..., STATE_JKY_SHIPPED]`

### P1 — Cron-c 取消被静默拦截

**命名**: 多态 → `JKY_CANCELLED` 缺口
**文件**: `cron_c.py:162` + `state_machine.py`
**场景**: 蓝盟取消订单 → cron-c 成功调 JKY cancel API → 状态转移被 state_machine 拒绝，但 `jky_unified` 和 `bridge_unified` 已被更新 → 数据不一致
**涉及路径**:
- `audited → jky_cancelled`
- `jky_shipped → jky_cancelled`
- `synced → jky_cancelled`
- `failed → jky_cancelled`
- `skipped → jky_cancelled`
**修复**: 对所有非终态添加 → `JKY_CANCELLED` 允许转移
**风险**: 当前 cron_c 更新 jky_unified/bridge_unified 在先、st_transition 在后，无原子性保护

---

## 4. 代码级隐患

### 4.1 静默失败不可见

`transition()` 在 `can_transition(from_state, to_state)` 返回 False 时 **静默 return False**，不记日志、不告警。

```python
if not can_transition(from_state, to_state):
    return False  # ← 无人检查返回值
```

所有调用方都未检查返回值。

**建议**: `can_transition()` 拒绝时至少 warning 日志，或直接 raise 异常 + catch wrapper。

### 4.2 部分更新原子性缺失（cron_c）

```python
# cron_c.py:160-162
conn.execute("UPDATE order_map SET jky_unified=..., bridge_unified=...")  # 先更新
conn.commit()
st_transition(row["id"], STATE_JKY_CANCELLED, "cron_c")  # 后状态转移
```

如果 st_transition 返回 False（非法转移），jky_unified 和 bridge_unified 已被更新但 state 未变 → 数据不一致。

**建议**: 将 unified 更新放进 transition() 的同一个事务中，或使用 state_machine 内置的 last_error 字段。

### 4.3 两种 `jky_cancelled` vs `cancelled` 字面字符串

cron_c.py:126 硬编码了 `"cancelled"` 字符串（小写 c），而 STATE_CANCELLED 是 `"cancelled"`（同样是 `cancelled`，区分大小写 — 从定义看是一样的？）。实际上两者相同，但不一致做法是脆弱编码风格。

```python
if row["state"] in (STATE_JKY_CANCELLED, "cancelled"):  # 硬编码字符串
    continue
```

**建议**: 所有硬编码状态字符串替换为 STATE_* 常量引用。

---

## 5. 修复后完整转移表（建议）

```python
_ALLOWED_TRANSITIONS = {
    STATE_INIT: [STATE_AUDITED, STATE_SKIPPED, STATE_CANCELLED, STATE_FAILED],
    STATE_AUDITED: [
        STATE_JKY_CREATED, STATE_JKY_SHIPPED,
        STATE_JKY_CANCELLED, STATE_SKIPPED, STATE_FAILED,
    ],
    STATE_JKY_CREATED: [
        STATE_JKY_SHIPPED, STATE_JKY_CANCELLED, STATE_DONE, STATE_FAILED,
    ],
    STATE_JKY_SHIPPED: [
        STATE_SYNCED, STATE_JKY_CANCELLED, STATE_FAILED,
    ],
    STATE_SYNCED: [
        STATE_DONE, STATE_JKY_CANCELLED, STATE_FAILED,
    ],
    STATE_DONE: [],
    STATE_FAILED: [
        STATE_INIT, STATE_AUDITED, STATE_JKY_CREATED,
        STATE_JKY_SHIPPED, STATE_JKY_CANCELLED,
    ],
    STATE_SKIPPED: [STATE_AUDITED, STATE_JKY_CANCELLED, STATE_FAILED],
    STATE_JKY_CANCELLED: [],
    STATE_CANCELLED: [],
}
```

---

## 6. 各调用方允许转移验证矩阵

| from \ to | init | audited | jky_created | jky_shipped | synced | done | failed | skipped | jky_cancelled | cancelled |
|-----------|------|---------|-------------|-------------|--------|------|--------|---------|---------------|-----------|
| **init** | — | cron-a | — | — | — | — | cron-a | cron-a | — | cron-a |
| **audited** | — | — | cron-a | webhook | — | — | cron-a | cron-a | cron-c/admin | — |
| **jky_created** | — | — | — | cron-b/webhook | — | cron-b? | cron-b | — | cron-c | — |
| **jky_shipped** | — | — | — | — | cron-b | — | cron-b | — | cron-c | — |
| **synced** | — | — | — | — | — | cron-b | cron-b | — | cron-c | — |
| **done** | — | — | — | — | — | — | — | — | — | — |
| **failed** | (admin?) | admin | admin | webhook | — | — | — | — | cron-c/admin | — |
| **skipped** | — | cron-a/admin | — | — | — | — | cron-c | — | cron-c | — |
| **jky_cancelled** | — | — | — | — | — | — | — | — | — | — |
| **cancelled** | — | — | — | — | — | — | — | — | — | — |

### 行/列交叉验证说明

- **jky_created → done**: 搜索代码未找到直接调用。cron_b 先 synced 再 done 双重调用是通过 `synced` 中转。✅ 实际路径: jky_created→jky_shipped→synced→done
- **failed → init**: 搜索代码未找到直接调用。admin 重置失败订单走 `failed → audited` 或 `failed → jky_created`。移除或保留均可。
- **skipped → failed**: cron-c 取消 skipped 订单时需要 → jky_cancelled。但如果没有 jky_trade_no → cron_c 跳过。加 `skipped → failed` 作为兜底。

---

## 7. 修复清单及优先级

| 优先级 | Bug | 影响范围 | 文件 |
|--------|-----|----------|------|
| P0 | AUDITED→SKIPPED | 生产已触发 | state_machine.py ✅ 已修 |
| **P1** | FAILED→JKY_SHIPPED + AUDITED→JKY_SHIPPED | webhook 回传断裂 | state_machine.py + app.py 引用 |
| **P1** | 多态→JKY_CANCELLED 缺口 | cron-c 取消后数据不一致 | state_machine.py |
| P2 | 静默失败不可见（transition return False 无日志） | debugging 困难 | state_machine.py |
| P2 | cron_c 部分更新原子性 | 数据不一致 | cron_c.py |
| P3 | 硬编码 `"cancelled"` 字符串 | 编码风格 | cron_c.py |
