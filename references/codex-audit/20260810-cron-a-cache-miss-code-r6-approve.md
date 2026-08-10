# Codex 代码审计 — cron-a cache-miss 自恢复 (第六轮, APPROVE)

- **日期**: 2026-08-10
- **阶段**: 步骤⑤ Codex 审计代码（第 6 轮，复审第 5 轮 REJECT 项）
- **范围**: 生产验证修复（去 warehouseCode 硬编码 + 分页拉全 + 跨仓档案一致性）
- **结论**: **APPROVE** — 无阻断项

## 第五轮 REJECT 项复审对照

| # | 级别 | 第五轮发现 | 修复 | 第六轮验证 |
|---|------|-----------|------|-----------|
| H-1 | High | pageSize=5 分页截断 → 跨仓冲突漏检（1802025032701 实际 12 仓，pageSize=5 只见 5 仓） | 分页循环拉全：pageIndex 递增 + `_PAGE_SIZE=20` + 末页不满即停 + `_MAX_PAGES=10` 防呆上限 halt | ✅ 闭合 — cron_a.py:318 分页拉取、:325 code!=200 fail-closed、:338 末页即停；stock_resp 无作用域未定义风险 |
| M-1 | Medium | 测试内联复制校验逻辑 → 与生产脱钩 | 抽模块级 `_resolve_archive()` 纯函数，生产与测试共用 | ✅ 闭合 — cron_a.py:432 调用、test:163 调用同一函数；无残留内联副本；边界（空/非dict/无匹配/跨仓冲突/多仓一致）全符合预期 |
| Low-1 | Low | 注释"固定创单仓 02 + 唯一匹配校验"过期 | 更新为"不传仓库条件，分页拉全 + 跨仓档案一致性校验" | ✅ 闭合 |
| Low-2 | Low | 重试测试仍构造 warehouseCode=02 | 已去掉 | ✅ 闭合 |

## 第六轮新增 Low（非阻断，已修复）

分页防呆文案"超过 200 条"不精确（连续满 10 页即 halt = 达到 200 条即 halt，未探第 11 页）→ 已改为"达到 200 条上限（连续 10 页满页）"。

## 验证记录

```
python tests/test_cache_miss_recovery.py → 10/10 PASS
python tests/test_state_machine_atomic.py → 9/9 PASS（脚本式测试，pytest 不收集属正常）
py_compile 全部通过
```

## 轮次时间线（累计）

1. 第一轮 REJECT — 7 High
2. 第二轮 REJECT — 4 High
3. 第三轮 REJECT — 1 Medium (M-3) + 2 Low
4. 第四轮 **APPROVE**（部署）
5. 第五轮 REJECT（生产验证后）— 1 High (H-1 分页) + 1 Medium (M-1 测试脱钩) + 2 Low
6. 第六轮 **APPROVE** ✅（再部署）

## 核心教训

**静态审计（Codex 4 轮 0 阻断）≠ 生产无 bug**：warehouseCode 硬编码与分页截断只有真实调用 `erp.stockquantity.get` 才能暴露——"部署前审计 vs 生产环境实测"双层验证缺一不可。
