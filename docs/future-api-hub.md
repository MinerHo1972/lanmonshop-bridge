# api-hub 抽象（未来方向 / 不实施）

> **状态**: 长期方向存档（2026-06-26 拍板 = 本次不实施）
> **触发条件**: 第 3 个 API 接入需求出现时重新评估
> **设计原则**: 减法 + Rule of Three（3 个实例出现再抽象）
> **owner**: hermes

---

## 1. 动机

当前 2 个 API provider 各自硬编码鉴权 + 调用逻辑，存在重复实现 4 类能力：

| 能力 | jky (hermes-web-api) | 中台 (lanmonshop-bridge 实施中) |
|---|---|---|
| 凭证管理 | token (env) | appKey + sign (settings.yaml) |
| 鉴权签名 | 内置 token 透传 | 自实现 sign 计算 |
| HTTP 调用 | aiohttp wrapper | 待实现 |
| 调用审计 | 无 | 待 call_logs schema |

第 3 个 API 接入时若仍按此模式，将出现 3 处独立重复实现。

## 2. 抽象层次

api-hub = framework + provider 模式：

```
api-hub (framework)
  ├── 通用能力
  │   ├── 凭证管理（加密存储 / .env 注入 / 不入 commit）
  │   ├── 鉴权抽象（每 provider 自实现 + 统一调用入口）
  │   ├── HTTP 调用（统一超时 / 重试 / backoff / rate limit）
  │   └── call_logs 审计 schema（source/target/request/response/status/latency/error）
  └── provider 实现
      ├── jky_provider（吸收 hermes-web-api /jky/* 路由群）
      ├── lanmonshop_provider（吸收 lanmonshop-bridge 中台调用）
      └── future_provider_xxx
```

业务服务（lanmonshop-bridge）通过统一 provider 接口调用 api-hub，**不持有鉴权凭证、不做 HTTP 客户端**。

## 3. 拒绝理由（本次不实施）

1. **Rule of Three 反模式防御**：当前仅 2 个 provider，"为复用而设计"过度抽象 = 增加间接层 + 模糊边界
2. **减法原则穿透**：已生产验证方案（jky_provider 散落在 hermes-web-api）优先，不在已闭环系统推倒重来
3. **JKY Gateway 改造风险**：现有 /jky/* 路由是 launch-tracker 公共组件，迁出影响 launch-tracker 等依赖
4. **抽象层设计未验证**：provider interface 字段未实战，仅凭"应该这样设计"易出现 YAGNI 漏洞
5. **本次决策窗口已关闭**：2026-06-26 hermes 拍板"长期计划不该草率决定"，需观察 ≥ 1 季度再评估

## 4. 触发条件（满足任一即重新评估）

- 第 3 个 API 接入需求出现（不含 jky + 中台）
- 现有 provider 间出现 ≥ 3 处可复用代码片段
- 跨 agent 共享 api-hub 需求出现
- 任意 provider 的鉴权/调用代码出现安全事件（凭证泄漏 / 调用越权）

## 5. 决策依据

**会话**: 2026-06-26 飞书 DM（hermes ↔ Claude）
**上下文**: lanmonshop-bridge 推进到 PRD v0.2 完成阶段后，hermes 提议"把接入层拆为通用 api-hub + 业务服务双层"
**评估**: 长期方向正确（多 API 接入复用），但本次 session 草率决定风险高（影响 scope 3 拆解 + ECS 资源 + 抽象层未实战验证）
**决议**: 本次按原 6 scope 推进（两个实例方式 = jky_provider 保持现状 + 中台_provider 在 lanmonshop-bridge 实施中），api-hub 抽象存档为未来方向

## 6. 关联文件

- `../PRD.md` v0.2 §4.9 JKY Gateway 改造方案（决策点 D1 候选答案之一）
- `../PRD.md` v0.2 §7.1 关键外部依赖（凭据管理参考）
- `~/.hermes/skills/devops/agently-cli` skill（OAuth device flow + 两阶段 send 参考实现）
- `~/.hermes/memories/MEMORY.md`（场景持久化层）
- scene block `技术运维-OpenAgents基础设施.md` api-hub 段（pointer）

## 7. 重新评估 checklist（触发后跑）

```bash
# 1. 现状盘点
ls ~/.hermes/skills/devops/jackyun-api/    # jky provider 实现
ls ~/projects/lanmonshop-bridge/clients/   # 中台 provider 实现（实施完成后）

# 2. 重复代码片段审计
rg -tpy "sign\(|md5\(|hmac\(" \
  ~/.hermes/skills/devops/jackyun-api/ \
  ~/projects/lanmonshop-bridge/clients/ 2>/dev/null | wc -l

# 3. provider 数量确认
# 若 ≥ 3 → 进入 design doc 阶段 + PRD v0.4（含抽象层设计 + schema 设计 + 部署形态）
# 若 < 3 → 维持现状 + 更新本文档 trigger 状态
```
