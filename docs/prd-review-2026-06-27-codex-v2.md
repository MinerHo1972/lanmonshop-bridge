# PRD v0.3.4 二轮 Review (codex)

> reviewer: codex (gpt-5.3) · 2026-06-27 · 对象 `PRD.md` commit de3c0aa (784 行, 只读)
> 方法: 验证 v0.3.3 review (t_228f71a7) 11 项 findings 修复状态 + v0.3.4 新增内容 (P10/cron-f/alert_counter/cache_changes) 审查

## 1. 前一轮 11 项 Verify 表

| # | 级 | Finding | 状态 | 证据 |
|---|---|---|---|---|
| 1 | P0 | 路径 B/C webhook 归属三处互斥 | ✅ 已修复 | 统一「路径 W(webhook)/A(cron-b poll)」标签; L189 显式废弃「我们不参与」; P10 (L557) 拍板两路径都落地 hermes-web-api |
| 2 | P1 | cron-b 1min vs 60min 四处冲突 | ⚠️ 部分 | L30/L91/L321 已改 60min; **§4.7 API 表 L239 仍写「cron-b 1min」** |
| 3 | P2 | 版本头与 cron 计数陈旧 | ⚠️ 部分 | header v0.2→v0.3.3 (原 finding 已解); 但 header 未 bump v0.3.4; §2 L27「5 cron」实际 6(cron-f);「7 表」实际 8(alert_counter 未计) |
| 4 | P1 | jky_logistic_cache schema 缺失 | ✅ 已修复 | §11.2.6 L619 完整 DDL (jky_logistic_no/name/raw_json/fetched_at + index) |
| 5 | P1 | webhook 接收路径无拍板 | ✅ 已修复 | P10 L557 明确「两路径都做 + 都落地 hermes-web-api」, 含幂等/共享逻辑决策 |
| 6 | P1 | cron-e 时刻未钉 + 写锁碰撞 | ✅ 已修复 | 钉 02:30 CST, 与 cron-d 错峰 30min (L34, L633) |
| 7 | P1 | 单 worker 串行未定义 | ✅ 已修复 | L39/L207/L729: `max_instances=1` + SQLite WAL + busy_timeout |
| 8 | P1 | 三方对账无 owner | ✅ 已修复 | cron-f @03:30 CST (L694), 偏差>5 单→P0, 含 SQL |
| 9 | P2 | P2→P1 升级无存储载体 | ✅ 已修复 | alert_counter 表 (L682: exception_class/window_start_ts/count/upgraded_to_p1_at) |
| 10 | P1 | cache TRUNCATE 销毁历史 | ✅ 已修复 | §11.2.8 双 cache_changes 表 + soft-delete+diff-INSERT 算法 (L648) |
| 11 | P2 | source 枚举缺 webhook | ✅ 已修复 | L137 加 webhook + cron_d/e/f |

**小计: 9/11 完全修复, 2/11 部分.**

## 2. v0.3.4 新增内容 — 新 Finding

- **[N1 / P1] D-C 验签算法未具体化**。P10 落地 hermes-web-api 拍板清晰, 但 L31/L189/L719 多处「验签 (按 user 给 D-C 算法)」仅引用, 全文无验签字段/算法 (HMAC?RSA?签名串拼接顺序?)/重放防御 (时间戳窗口/nonce) 描述; §7.1 外部依赖表也未列入「webhook 验签密钥/算法」。webhook handler 无法据此实现验签逻辑。fix: §4.5 补验签伪代码 + §7.1 #9 加 D-C 验签密钥为外部依赖。
- **[N2 / P1] cron-d/e 描述与 §11.2.8 修正矛盾**。§11.2.3 L593 cron-d 仍写「全量刷新 (TRUNCATE + INSERT in transaction)」, §11.2.7 L635 cron-e 同样写 TRUNCATE; 但 §11.2.8 修正声明「TRUNCATE → soft-delete + diff-INSERT」。修正仅追加未回填, 追加不回填模式再次发生。fix: L593/L635 改写为 soft-delete+diff。
- **[N3 / P2] §2 汇总计数二次陈旧**。L27「5 cron」应为 6 (cron-f),「7 表」应为 8 (alert_counter); 与 #3 同类 staleness。
- **[N4 / P2] cron-f 对账范围与 cron-c 职责边界未划清**。cron-f (L699) 仅扫 jky_shipped/synced 两态, 未覆盖 jky_created (中台已退但吉客云已创未发); 该态现由 cron-c 单向覆盖, 但 cron-f 三方对账是否应含此态未声明。

P10 落地细节评估: state 校验充分 (L207 UNIQUE + state machine 不重复回传); 重复回传防御充分 (幂等三重保证); **验签算法是唯一硬缺口 (N1)**。

## 3. 总结

**修复完成度: 9/11 完全修复 (81.8%), 计 partial 为 10/11 (90.9%)。无新 P0。**

全部架构阻塞项 (P0 webhook 归属 + P1 schema/并发/审计/对账) 均已解, 骨架可启动。残余 2 项 partial (#2 API 表 1min 残留 / #3 汇总计数) + 2 项新 P1 (N1 验签算法 / N2 TRUNCATE 描述矛盾) 均为文档一致性 + 验签细节, 不阻塞实现启动, 但 N1 在 webhook handler 编码前必须补。

**仍存在风险**: ① N1 D-C 验签算法缺失 — webhook 主路径 handler 编码前硬阻塞; ② N2 TRUNCATE 描述矛盾 — 开发者读 §11.2.3 会误用 TRUNCATE; ③ #2/#3 term-sweep 残留 — 低风险但 30min 可清零。

**结论: PASS (有条件)** — 满足「修复≥90% + 无新 P0」; 建议编码前 15min term-sweep (L239 1min→60min + §2 计数 + L593/L635 TRUNCATE→soft-delete) + §7.1 补 D-C 验签密钥依赖。
