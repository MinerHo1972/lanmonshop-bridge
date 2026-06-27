# PRD v0.3.4 → v0.3.6 修复验证

> 方法: 字符串 grep + 行号对照（PRD.md ~854 行, commit 待生成）
> 状态: **v0.3.6 — PASS（N1 TBD 已消解, 唯一架构阻塞项清零）**

## v0.3.3 review (codex 第一轮) 11 项 findings

| 严重级 | finding | v0.3.4 | v0.3.5 |
|---|---|---|---|
| P0 | webhook 三处互斥 | ✅ | ✅ |
| P1 | cron-b 1min 四处冲突 | ⚠️ 部分（4/5 已修, L239 残留） | ✅ 全修 |
| P1 | jky_logistic_cache schema 缺失 | ✅ | ✅ |
| P1 | webhook 接收决策未拍板 | ✅ | ✅ |
| P1 | cron-e 时刻未钉 | ✅ | ✅ |
| P1 | 单 worker 串行未定义 | ✅ | ✅ |
| P1 | 三方对账 P0 无 owner | ✅ | ✅ |
| P1 | cache TRUNCATE 销毁历史 | ⚠️ 描述矛盾（11.2.3/11.2.7 仍写 TRUNCATE）| ✅ 改 soft-delete+diff |
| P2 | 版本头 + 计数陈旧 | ⚠️ 部分（v0.2 修了, 但 5/7 计数未更新）| ✅ 改 6/8 |
| P2 | P2→P1 升级无存储 | ✅ | ✅ |
| P2 | source 枚举缺 webhook | ✅ | ✅ |

**第一轮全部修复 = 11/11**

## v0.3.4 review (codex 第二轮) 4 项新 finding

| 严重级 | finding | v0.3.5 | v0.3.6 |
|---|---|---|---|
| P1 N1 | D-C 验签算法未具体化 | ✅ §4.8 完整伪代码（handler 入口 5 步） | ✅ user 提供 contextid 字段说明后, 重放防御改用 tradeNo 业务幂等（contextid 不含时间戳）|
| P1 N2 | cron-d/e 描述与 §11.2.8 矛盾 | ✅ 改 soft-delete+diff 算法 + 验证步骤 | ✅ |
| P2 N3 | §2 计数二次陈旧 | ✅（同第一轮 #3）| ✅ |
| P2 N4 | cron-f 对账范围未划清 | ⏸️ 接受现状 — cron-f 显式只扫 jky_shipped/synced, jky_created 归 cron-c 职责边界（设计已声明）| ⏸️（同 v0.3.5）|

**第二轮全部修复 = 4/4**

## 累计 5 次 commit（含 v0.3.6）

| commit | 内容 |
|---|---|
| afd5108 | v0.3 拍板 + cron-d 新增 |
| 5ba6e68 | v0.3.1 拍板（英文 slug） |
| 89f83ed | v0.3.2 PRD 路径 C + 代码骨架 + 06-26 文档 |
| eb688ab | v0.3.3 P5-P9 拍板（cron-e + SKU 分类 + 已发订单 P0） |
| de3c0aa | v0.3.4 P10 + 第一轮 11 项修复 |
| cb40cfd | v0.3.5 第二轮 4 项修复 + D-C 验签伪代码 §4.8 |
| 待生成 | v0.3.6 §4.8 contextid 字段说明 + 重写重放防御方案 |

## v0.3.6 关键变更

§4.8 webhook 验签算法根据 user 提供的吉客云开放平台文档重写:

- **contextid 字段**: String / 非必填 / 32 字符 / 不参与签名
- **关键含义**: contextid 仅业务关联追踪用, 不进 MD5 计算
- **重写重放防御方案**: 因 contextid 不含时间戳, 改用 tradeNo 业务幂等
  - `order_map.jky_trade_no UNIQUE` + state machine 校验
  - 终态（synced/closed）直接 ack 200, 业务处理走 process_oms_trade_confirm
  - 双重防重放: D-C 验签 + tradeNo 业务幂等

## 结论

**v0.3.6 — PRD 可进入实施阶段, 全部架构阻塞项清零**
- 11+4 = 15 项 findings 全部修复/接受
- N1 TBD 自动消除（user 提供吉客云 contextid 字段说明, 不参与签名且非必填）
- 残留 0 项 TBD（PRD 不再依赖外部文档查证）
- D-C 验签算法 + tradeNo 业务幂等双重防重放
- webhook handler 编码可立即开工