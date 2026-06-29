# Scope 0 基线 Verify — ECS 真实只读验证 (v0.3.6)

- **任务**: t_c65a562c
- **采集时间**: 2026-06-27 (真实 SSH verify, 非手动估算)
- **采集者**: hermes coding worker
- **ECS**: `root@8.153.195.8` (SSH 密钥 `~/.ssh/id_rsa_alicloud`)
- **commit**: 39591b8 v0.3.6
- **参考**: docs/scope-0-baseline-v036-2026-06-27.md (基线 source of truth) / PRD v0.3.6 §11.4 §8 Step 0
- **方法**: 单次 SSH 会话, 全部只读命令, 未启动任何额外服务 (无 cloudflared / 无 WSL 服务 / 未改动 ECS 服务)

> 本文件覆盖 codex 旧版 scope-0-verify (旧版误把 hermes-gateway/kanban-dashboard/cloudflared 列入 ECS 范围, 且 codex 曾 spawn cloudflared 污染基线数据)。

---

## 结论: SCOPE 0 PASS (v0.3.6 新标准, 4/4)

| # | 检查项 | v0.3.6 标准 | 实测 | 结果 |
|---|---|---|---|---|
| 1 | 内存 available | ≥ 800Mi | **927 Mi** | ✅ PASS |
| 2 | 2 服务 running | 2/2 | hermes-web-api + lanmonshop-bridge 均 active | ✅ PASS |
| 3 | 磁盘 / 剩余 | ≥ 5G | **19G** | ✅ PASS |
| 4 | health endpoint | 200/200 | 8088=200, 18433=200 | ✅ PASS |

---

## 1. 内存基线 (`free -h`)

```
              total        used        free      shared  buff/cache   available
Mem:          1.8Gi       763Mi       221Mi        15Mi       903Mi       927Mi
Swap:            0B          0B          0B
```

- **available = 927 Mi** (≥ 800Mi → PASS)
- total = 1.8 GiB, used = 763Mi, buff/cache = 903Mi
- **Swap = 0B** (无 swap, 1.8GiB 物理内存为硬约束)
- 当前可用 (`free -m | awk '/^Mem:/ {print $7}'`) = **927 MiB**

---

## 2. 服务基线 (scope 0 = 2 服务)

| 服务 | PID | RSS | 端口 | 状态 | etime |
|---|---|---|---|---|---|
| hermes-web-api (uvicorn main:app) | 280065 | ~58 MiB (59652 KB) | 8088 | ✅ LISTEN + 200 | 1-01:41 |
| lanmonshop-bridge (uvicorn lanmeng_bridge.app:app) | 281274 | ~60 MiB (61484 KB) | 18433 | ✅ active (systemd) + 200 | 1-01:37 |
| **小计** | | **~118 MiB** | | | |

### 端口监听确认 (ss -tlnp)

```
LISTEN 0  2048  0.0.0.0:8088   0.0.0.0:*  users:(("python3.11",pid=280065,fd=11))
LISTEN 0  2048  0.0.0.0:18433  0.0.0.0:*  users:(("python3.11",pid=281274,fd=14))
```

### systemd 状态

- `lanmonshop-bridge.service`: `is-active` = **active**, MainPID=281274, ActiveState=active
- `hermes-web-api`: 跑在 uvicorn background (非 systemd unit), 已由 `ps` + 端口监听 + health 三重确认

---

## 3. 磁盘基线 (`df -h /`)

```
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda3        40G   19G   19G  50% /
```

- 剩余 **19G** (≥ 5G → PASS)

---

## 4. Health Endpoint

```
http://127.0.0.1:8088/health  → http_code=200
http://127.0.0.1:18433/health → http_code=200
```

---

## 5. 系统信息

- OS: Alibaba Cloud Linux (kernel 5.10.134)
- uptime: up 9 days, 4:39
- load average: 0.02, 0.02, 0.00
- vCPU: 2 (per baseline doc)

---

## 6. 与基线 doc 对比 (drift)

| 指标 | baseline doc (19:30) | 实测 verify | drift | 说明 |
|---|---|---|---|---|
| available | 957 Mi | 927 Mi | -30 Mi | 正常波动 (buff/cache 回收动态), 仍 ≥ 800Mi |
| hermes-web-api RSS | 60 MiB | ~58 MiB | -2 MiB | 一致 |
| lanmonshop-bridge RSS | 61 MiB | ~60 MiB | -1 MiB | 一致 |
| df / 剩余 | 19G | 19G | 0 | 一致 |

**结论**: 基线 doc 数据可靠, 实测在容差范围内。available 927Mi vs doc 957Mi 属正常抖动。

---

## 7. v0.3.6 部署后预期 (per baseline doc §4.2, 本 verify 不部署)

```
部署前:  used 763Mi / available 927Mi
稳态:    used ~853Mi / available ~837Mi   (预计 ≥ 100MiB free)
峰值:    used ~913Mi / available ~777Mi   (预计 ~27MiB free + buff/cache 可回收)
```
风险: 峰值紧张但可控 (cron-d/e/f 已错峰 30min)。详见 baseline doc §4.3 OOM 应急。

---

## verify 清单 (实施时打勾)

- [x] `ssh ... 'free -h'` available ≥ 800Mi → **927Mi PASS**
- [x] 2/2 服务 running (hermes-web-api + lanmonshop-bridge)
- [x] `curl 8088/health` = 200 + `curl 18433/health` = 200
- [x] `df -h /` available ≥ 5G → **19G PASS**
- [x] `free -m | awk '/^Mem:/ {print $7}'` 记录 = **927 MiB**
- [ ] 部署 v0.3.6 后重跑 free -h, 验证 used ≤ 950Mi (本 verify 未部署, scope 0 仅基线)

**约束遵守**: ✅ 未启动 cloudflared ✅ 未验证 WSL 服务 ✅ 未改动 ECS 服务 (纯只读)

---

**End of scope 0 verify (real SSH read-only)**
