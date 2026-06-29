# PRD v0.3.3 结构化 Review

> reviewer: codex (gpt-5.3) · 2026-06-27 · 对象 `PRD.md` commit eb688ab (680 行, 只读)
> 方法: 5 维度验收集清单 · 仅 review, 不含代码实现

## 1. 一致性

- **[P0] 路径 B/C webhook 归属三处互斥**。§2 L31 称「路径 C = oms.trade.confirm webhook, 主路径, 我们实现 `/jky/webhook/oms.trade.confirm` → 验签 → 调中台」;§4.5 L184 称「oms.trade.confirm = 吉客云→中台直发, 中台自己接收, 我们**不**参与, 仅实现路径 A」;§4.5 L191 又称「路径 B = 同一 webhook, 我们验签后调中台」。三段对「我们是否建 webhook 接收方」给出 3 个互斥答案, 且 B/C 标签混用 (L617 又回称路径 C)。开发者无法据此判定建不建 handler。fix: 统一单一标签 (建议「webhook 路径 / 路径 W」), 删除或纠正 L184「我们不参与」语义, 明确接收方落地 hermes-web-api 还是 bridge。
- **[P1] cron-b 频次 1min vs 60min 四处冲突**。§2 L30 已改「60min 兜底」, 但 §4.1 L88 目录注释、§4.7 L230 API 表、§5.3 L312 端到端 SOP 仍写 1min。fix: 全文统一 60min 并修正 L312「1min 内监听」断言。
- **[P2] 版本头与 cron 计数陈旧**。L4 文档头仍标 v0.2, §2 L27 仍写「3 cron」, 实际 v0.3 已 5 cron (cron-d/e 见 §11)。fix: 头部 bump v0.3, §2 改「5 cron」。

## 2. 完整性

- **[P1] jky_logistic_cache 表 schema 全文缺失**。§4.6 L217、P5 L543、scope4 L641 多次引用 cron-e 刷新此表, 但通篇无任何 CREATE TABLE (对照 jky_product_cache §11.2.2 有完整 DDL)。cron-e 无法动工。fix: 补 §11.x DDL (至少 jky_logistic_no / name / raw_json / fetched_at)。
- **[P1] webhook 接收路径无拍板记录**。P1-P9 覆盖了 gateway 路由追加 (P2)、SKU 源 (P4)、物流源 (P5), 但「是否自建 webhook 接收 + 验签算法 (D-C) + 落地哪个服务」未进拍板表。依上面 P0 结论若需建接收方, 此决策点必须补拍。fix: 新增 P10 记录 webhook 接收归属。

## 3. 可行性

- **[P1] cron-e 触发时刻未钉, 与 cron-d 写锁碰撞**。cron-d 钉 02:00 CST (L580), cron-e 仅写「每日」(L217) 无时刻。两者均 TRUNCATE+INSERT cache 表, 同库并发写触发 `database is locked`。fix: 错峰 (如 cron-e 02:30) 或显式排队。
- **[P1] 「单 worker 串行」机制未定义**。用户故事 10 (L54) 要求同订单不被 cron-a/b/c 并发处理, 但未说明 APScheduler 如何保证 — 默认线程池并不串行。cron-a 与 cron-c 均 5min, 批量大时互相饿死。fix: 明确 `max_instances=1` + 全局 lock 或单进程 job queue。

## 4. 可测试性

- **[P1] P0「三方状态偏差 ≥5 单」无实施 owner**。§9.1 L394 列为 P0, 但全文无 cron 负责三方 (中台/吉客云/DB) 对账 — cron-c 只做单向 (中台 -2 → jky cancel)。该 P0 不可自动触发 = 不可测。fix: 指派对账 cron 或并入 cron-c 双向扫描, 给出计数 SQL。
- **[P2] P2→P1 升级计数器无存储载体**。§9.2 L427「30min 窗口连续 3 次」需按 exception class 聚合, 但 retry_count 是 per-order (L113), 无聚合表。fix: 加 alert_counter 表 (exception_class/window/count)。

## 5. 审计即架构

- **[P1] jky_product_cache TRUNCATE 销毁历史, 违反「审计即架构」一等公民**。§11.2.3 L583 全量 TRUNCATE+INSERT, 先前日快照丢失;raw_json (L567) 仅存当前值。order_map 有 order_status_log 留痕, 但两张 cache 表 (product + logistic) 均无 changes 表。task 明确要求审计字段一等公民 — 当前仅 order_status_log 达标。fix: 加 jky_product_cache_changes 表记 diff, 或改 TRUNCATE 为软标记+归档。
- **[P2] order_status_log.source 枚举未含 webhook**。L132 列 cron_a/b/c/manual/api/human_close, webhook 触发的状态写入无对应值 (归 api?)。fix: 显式加 'webhook'。

---

## 总结

整体: v0.3.3 业务闭环完整 (下载/回传/取消/SKU+物流双 cache)、拍板密度高、可执行性强, 但 v0.2→v0.3「追加不回填」策略 (L526) 造成主文档与新章节系统性不一致。风险 top3: ①[P0] 路径 B/C webhook 归属三处互斥, 直接阻塞接收方实施;②[P1] jky_logistic_cache schema 全文缺失, cron-e 无法动工;③[P1] 单 worker 串行机制未定义 + cron-e 时刻未钉, 上线易发 SQLite 写锁死锁。下一步: 30min 拍板会消解 P0 + 补 P10 webhook 决策, 全文 term-sweep 统一路径标签/cron-b 频次/版本头, 补两张 cache_changes 表落实审计承诺。
