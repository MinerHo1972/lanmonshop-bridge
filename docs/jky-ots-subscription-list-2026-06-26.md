# 吉客云 OTS 订阅清单 + 开通 SOP (2026-06-26)

> **关联**: lanmonshop-bridge PRD v0.3 §4.9 + §7.1 + §11.3 (D1=A 拍板) + §11.4 (scope 计划调整)
> **验证时间**: 2026-06-26
> **审计等级**: Schema-Auditor 一等公民 (method 清单 SSoT + 开通 SOP + scope 状态)

---

## 1. 待订阅 method 清单（4 个，scope 3 必需）

**吉客云 OTS 后台**: `https://open.jackyun.com`

| # | method 名称 | 中文用途 | 关联路由 (JKY Gateway) | 触发 cron | PRD 章节 |
|---|---|---|---|---|---|
| 1 | `oms.trade.ordercreate` | 创建销售单 | `/jky/trade/create` | cron-a (5min) | §4.7 + §4.9 + §11.3.1 |
| 2 | `oms.trade.audit.pass` | 销售单审核 | `/jky/trade/audit` | cron-a (5min) | §4.7 + §4.9 + §11.3.1 |
| 3 | `oms.trade.ordercancel` | 销售单取消 | `/jky/trade/cancel` | cron-c (5min, 对账) | §4.7 + §4.9 + §11.3.1 |
| 4 | `erp-goods.goods.sku.search` | 商品列表查询 | `/jky/goods/list` | cron-d (1/day, 02:00 CST) | §4.9 (v0.3 新增) + §11.2.3 + §11.3.1 |
| 5 | `oms.trade.confirm` | 吉客云→外部系统发货回传 (webhook callback) | `/jky/webhook/oms.trade.confirm` (E2 拍板) | 🆕 路径 C 主路径 (user 2026-06-26 拍板) | §4.5 路径 C + §4.7 + §4.9 + §11.3.1 |

**吉客云 callback 验签算法** (user 2026-06-26 拍板 D-C):
- 步骤 1: 取除 `sign` 和 `contextid` 参数外的所有"参数+参数值"
- 步骤 2: **字典排序**生成字符串
- 步骤 3: `AppSecret` 加到字符串**首尾**（`secret + sorted_str + secret`）
- 步骤 4: 整个字符串**转小写**
- 步骤 5: **MD5 加密**
- 步骤 6: 结果 = `Sign` 字段

**注**: 此算法**不**等同于蓝盟中台 sign 算法（蓝盟 = `md5(appKey & timestamp & appSecret).toUpperCase()`，大写）= 两套独立 sign 凭据。credentials.yaml 需新增吉客云 callback AppSecret 字段。

**注意**: 本表 5 method = 我们要做的事（4 个我们主动 poll/push + 1 个我们被动接收 webhook）。"未列入但相关"段已删除（见下）。

**用户拍板的设计认知**（user 2026-06-26）:
- `oms.goods.query` PRD 推测错位 = 真实 `erp-goods.goods.sku.search`（v0.3 已修正）
- `oms.trade.confirm` = 真实 method = 吉客云→外部系统发货回传（user 提议作 cron-b 1min poll 替代 = 路径 C 待拍板）

**待 verify（user OTS 后台核对）**:
- 1/2/3 method 真实前缀（`oms.trade.*` vs `erp-trade.*`）
- 我们 cron-b 拉已发货订单 method（`oms.trade.confirm` 作 callback 替代时，本 method 就不需要了 = cron-b 删/改兜底）

**注意** (PRD §4.9 提示): 未订阅 = 业务 cron 全失败，返回 `subCode: 0130020310`。开通后**必须**端到端真验。

---

## 2. 吉客云 OTS 后台开通 SOP (4 步)

### Step 1 — 登录 OTS 后台
- URL: `https://open.jackyun.com`
- 账号: 复用 launch-tracker 已有吉客云账号体系

### Step 2 — 进入"API 订阅"页面
- 找 method 订阅入口
- 用关键词搜 4 个 method: `trade.ordercreate` / `trade.audit.pass` / `trade.ordercancel` / `goods.query`

### Step 3 — 逐个申请订阅
- 每个 method 点"申请订阅" / "开通"
- 需审批的等审批通过
- 状态 = 已订阅 即生效

### Step 4 — 端到端真验（scope 3 实施前必跑）
- 我在 hermes-web-api 加 4 路由后跑 curl 验证
- 验证命令（举例）: `curl -X POST http://8.153.195.8:8088/jky/trade/create -d '{...}'`
- 期望: HTTP 200 + 业务响应（非 subCode 错误）
- 失败: HTTP 401/403/subCode → 检查 OTS 订阅状态

---

## 3. scope 1 sign 算法调研启动 (待 user 推进)

PRD v0.3 §4.4 字段映射表提"中台 appKey + sign 鉴权"，但**sign 算法未详**。scope 1 需 user 推进 4 项 verify。

按"AI 不允许凭想象虚构硬规则"硬约束（2026-06-26 user 拍板）= sign 算法**不**由 AI 凭空猜，必须 user 拿一手文档。

### 待 verify 1: sign 算法
- 算法: HMAC-SHA256? MD5? 其它?
- 字段: sign = ?(appKey + timestamp + body)?
- timestamp 格式: 毫秒? 秒? ISO8601?
- 来源: 蓝盟对接人 / 中台 API 文档 / 试错

### 待 verify 2: ordercancel 适用范围
- PRD §7.1 #8: 未审/待审/已发/已签收各状态能否取消?
- 已发/已签收的取消可能要售后流程
- 来源: 吉客云 OTS 文档 / 测试

### 待 verify 3: 吉客云物流公司编码清单
- PRD §7.1 #2: 物流编码
- 用于 `logistic.yaml` 配置 (PRD §4.6)
- 来源: 吉客云 OTS 文档 / API 拉取

### 待 verify 4: cron-d goods/list method 准确名
- PRD §11.2.3 写"待 verify = oms.goods.query"
- 可能实际名 = `goods.list.query` / `goods.query.list` / 其它
- 来源: 吉客云 OTS 文档 / API 试错 (你 OTS 后台订阅时确认 method 精确名)

---

## 4. scope 0 + scope 1 状态

### scope 0 (ECS 资源基线 — 已闭环)
- ✅ 2026-06-26 14:15 CST 闭环
- 详见 `docs/baseline-2026-06-26.md`

### scope 1 (外部依赖 verify — 部分闭环)
- ✅ 蓝盟 appKey + appSecret 入 credentials.yaml v4 (测试密钥，待正式)
- ⏸️ 吉客云 OTS 4 method 订阅 (user 2026-06-26 推进中)
- ⏸️ sign 算法 verify (user 2026-06-26 推进中)
- ⏸️ ordercancel 适用范围 verify (user 2026-06-26 推进)
- ⏸️ 吉客云物流公司编码清单 (user 2026-06-26 推进)
- ⏸️ cron-d goods/list method 准确名 verify (user 2026-06-26 推进)

### scope 1 实施 SOP (user 推进 + 我实施)
1. user 去 OTS 后台开通 4 method (§2 Step 1-3)
2. user 反馈 sign 算法文档 (§3 待 verify 1) + ordercancel 范围 (§3 待 verify 2) + 物流编码 (§3 待 verify 3) + cron-d method 准确名 (§3 待 verify 4)
3. 我立即在 hermes-web-api 上加 4 路由 (PRD §4.9 实施步骤)
4. 端到端真验 (§2 Step 4)
5. 跑通 = scope 1 部分闭环
6. 失败 = 排查 + retry (按"3-option 终止 pattern"不增加新 option)
