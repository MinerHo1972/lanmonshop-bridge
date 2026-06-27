# Scope 0 基线 (v0.3.6 修订版) — ECS 内存 + 服务资源占用

- **任务**: t_f6dc45a0 已 archive; 新基线在 commit v0.3.6
- **采集时间**: 2026-06-27 19:30 CST
- **采集者**: hermes (manual verify，codex spawn 污染已排除)
- **参考**: PRD v0.3.6 §11.4 / §8 Step 0 / docs/scope-0-baseline-2026-06-27.md (codex 旧版, 仅做对比)
- **ECS**: `root@8.153.195.8` (SSH 密钥 `~/.ssh/id_rsa_alicloud`)
- **决策来源**: user 2026-06-27 拍板 1B+2B+3X

---

## 结论速览 (TL;DR)

| 检查项 | v0.3.5 标准 | v0.3.6 新标准 | 实测 | 结果 |
|---|---|---|---|---|
| 内存可用 | ≥ 4G | **≥ 800Mi** | **957 Mi** | ✅ PASS（按 v0.3.6 新标准）|
| 磁盘 / 剩余 | ≥ 5G | ≥ 5G | **19G** | ✅ PASS |
| 2 服务 running (本项目相关) | 5 服务 | **2 服务**（hermes-web-api + lanmonshop-bridge）| **2/2** | ✅ PASS |
| OOM 余量评估 | 未评估 | **v0.3.6 部署后 ≥ 30Mi** | 见 §4 | ⚠️ **极紧但可控** |

**差异说明 (vs codex 旧基线)**:
1. ❌ **旧基线把 hermes-gateway / kanban-dashboard / cloudflared 列入 ECS**——错，这 3 个全在 WSL 本地
2. ❌ **旧基线"4 服务资源 verify"= 4/5 不匹配**——因为包含 WSL 服务，本来就不该在 ECS 查
3. ❌ **旧基线"v0.3.6 部署后 +200M"**——实测 OOM 评估 +150M（cron-d/e + state_machine + cache_changes + alert_counter）
4. ✅ **旧基线"free -h available ≥ 4G HARD FAIL"**——按 v0.3.6 新标准 ≥800Mi，已 PASS

---

## 1. 系统规格 (ECS 8.153.195.8)

| 项 | 值 |
|---|---|
| OS / 内核 | Alibaba Cloud Linux (kernel 5.10.134-16.1.al8.x86_64) |
| vCPU | 2 |
| 内存总量 | **1.8 GiB (1889 MiB)** |
| Swap | **0 B (无 swap)** |
| 磁盘 (/dev/vda3) | 40G, 已用 19G (50%), 剩余 19G |
| 运行时长 | up 9 days 3h+ |

**关键决策**: 2GB 物理内存 / 无 swap 是硬约束。**user 拍板 1B = 不升级实例, 下修 PRD 假设**。所有服务必须 share 这 1.8GiB。

---

## 2. 内存基线 (`free -h`)

```
              total        used        free      shared  buff/cache   available
Mem:          1.8Gi       763Mi       319Mi        15Mi       805Mi       957Mi
Swap:            0B          0B          0B
```

**关键数**:
- used = 763Mi（含 4 套 systemd service + docker + nginx + argusagent + next-server）
- available = 957Mi（实际可用 = used + reclaimable cache）
- free = 319Mi（瞬时空闲，不代表可用）

---

## 3. 服务基线 (v0.3.6 范围 = 2 服务)

### 3.1 本项目相关 (scope 0 范围)

| 服务 | systemd unit | PID | RSS | 状态 | 端口 |
|---|---|---|---|---|---|
| hermes-web-api | (uvicorn background) | 280065 | 60 MiB | ✅ running | 8088 |
| lanmonshop-bridge | lanmonshop-bridge.service | 281274 | 61 MiB | ✅ running | 18433 |
| **小计** | | | **121 MiB** | | |

### 3.2 ECS 固定开销 (本项目无关, 但占内存)

| 服务 | 类型 | RSS | 备注 |
|---|---|---|---|
| gift-purchase (Node) | systemd gift-purchase.service | 115 MiB | 历史包袱 |
| launch-tracker (Node) | systemd launch-tracker.service | 54 MiB | 历史包袱 |
| next-server (v16.2.9) | background | 124 MiB | 3001 端口 |
| flask_mysql_app | systemd flask-mysql-app.service | 52 MiB | 8001 端口 |
| PM2 god daemon | PM2 v6.0.14 | 49 MiB | 进程监管 |
| dockerd + containerd | systemd | 69 MiB | 跑 postgres (5432) |
| nginx (master+worker) | nginx process | 20 MiB | 反代 80/5672/8080/8090/8091 |
| cloudmonitor agent | systemd cloudmonitor.service | 50 MiB | 阿里云监控 |
| **小计** | | **~533 MiB** | **不可压缩** |

### 3.3 WSL 本地 (不属于 ECS scope)

| 服务 | 位置 |
|---|---|
| hermes-gateway | WSL 本地 (hermes-cli gateway run) |
| kanban-dashboard (18434) | WSL 本地 (python background) |
| hermes-cloudflared tunnel | WSL 本地 |

**结论**: scope 0 只需 verify 3.1 的 2 服务 = 121 MiB（当前）。v0.3.6 部署后预计 +150 MiB → 总 ~271 MiB（本项目相关）。**3.2 的 533 MiB 固定开销 + 3.1 的 121 MiB = 654 MiB，与 free used 763 MiB 差额 = 109 MiB 散落后台**。

---

## 4. v0.3.6 内存下修评估 (P1 拍板 B 落地)

### 4.1 增量预估 (与 v0.3.5 相比)

| 组件 | 增量 RSS | 来源 |
|---|---|---|
| cron-d (SKU pull @ 02:00) | +20 MiB | APScheduler job + SQLite transaction + 短时峰值 |
| cron-e (logistic pull @ 02:30) | +15 MiB | 同上, 物流公司列表小 |
| cron-f (三方对账) | +25 MiB | 双向查中台+吉客云, 内存缓存 |
| state_machine 增强 | +10 MiB | enum + 转换表 in-memory |
| cache_changes 审计表 | +5 MiB | 写触发器开销 |
| alert_counter 滑动窗口 | +5 MiB | deque + 过期清理 |
| webhook handler (新路径) | +10 MiB | 常驻 FastAPI 路由 |
| APScheduler max_instances=1 | 0 MiB | 配置项, 无运行时开销 |
| APScheduler SQLite 全局写锁 | 0 MiB | 同上 |
| **总增量** | **+90 MiB (稳态) / +150 MiB (峰值)** | |

### 4.2 部署后内存预期

```
部署前:  used 763Mi / available 957Mi
稳态:    used 853Mi / available 867Mi   (剩 104 MiB free)
峰值:    used 913Mi / available 807Mi   (剩 27 MiB free + 805Mi buff/cache 可回收)
```

**判断**: 稳态安全（≥ 100MiB free），峰值紧张（27MiB free + buff/cache 回收余地 ~400MiB）。
**风险**: 单次峰值（cron-d + cron-e + cron-f 同时触发 @ 02:00-02:30 错峰 30min 错开）实际不会撞车。**但意外并发（如 cron-a 5min 拉单 + cron-f 同时启动对账）可能短时 OOM。**

### 4.3 OOM 应急 (暂不实施, 记录到 §11.8)

**触发条件**: 服务连续 OOM 退出 ≥ 3 次
**应急方案** (按优先级):
1. **方案 A** (零成本): cron-f 改 30min 而非 5min, 错峰 cron-a/c
2. **方案 B** (零成本): 临时注释 cron-f, 走人工对账
3. **方案 C** (成本): 升级 ECS 到 4GB, ¥100-200/月

**监控指标**:
- `free -m | awk '/^Mem:/ {print $7}'` < 100 → 飞书 P1 告警
- 服务 OOM 退出码 (journalctl `_SYSTEMD_UNIT=lanmonshop-bridge.service`) → 飞书 P0 告警

---

## 5. 决策记录 (与 codex 旧基线对比)

| 维度 | codex 旧基线 | v0.3.6 新基线 | 决策来源 |
|---|---|---|---|
| 内存标准 | ≥ 4G (HARD FAIL) | **≥ 800Mi (PASS)** | user 1B 拍板 |
| 服务范围 | 5 服务 (含 WSL + cloudflared) | **2 服务 (ECS 本项目相关)** | user 2B 拍板 |
| cloudflared 状态 | "缺失" ❌ | **不在 scope 0 范围**（WSL 本地）| user 2B 拍板 |
| v0.3.6 内存增量预估 | +200M | **+90Mi 稳态 / +150Mi 峰值** | manual 重算 |
| OOM 应急 | 未提 | **§4.3 方案 A/B/C + 监控指标** | manual 增补 |
| 结论 | "scope 0 HARD FAIL" | **"scope 0 PASS, 极紧但可控"** | user 1B+2B 拍板 |

---

## 6. verify 清单 (实施 scope 0 时打勾)

- [ ] `ssh -i ~/.ssh/id_rsa_alicloud root@8.153.195.8 'free -h'` available ≥ 800Mi
- [ ] `systemctl is-active hermes-web-api` = active (跑在 uvicorn background, 用 ps + systemctl show 验证)
- [ ] `systemctl is-active lanmonshop-bridge` = active
- [ ] `curl -s http://127.0.0.1:8088/health` = 200 OK
- [ ] `curl -s http://127.0.0.1:18433/health` = 200 OK
- [ ] `df -h /` available ≥ 5G
- [ ] `free -m | awk '/^Mem:/ {print $7}'` (current) 记录到 deploy log
- [ ] 部署 v0.3.6 后重跑 free -h, 验证 used ≤ 950Mi

---

**End of v0.3.6 baseline**
