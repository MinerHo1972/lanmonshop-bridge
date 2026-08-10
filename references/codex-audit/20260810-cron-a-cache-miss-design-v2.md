# cron-a cache-miss 自恢复设计（修订版 v2）

> 状态: 待 Codex 审计（六步流程步骤③）
> 日期: 2026-08-10
> 前置: Codex 审计 v1 发现 7 个高风险项（H1-H7），用户拍板 H1/H2 决策；H3 经生产实测彻底闭合

## 背景

jky_product_cache 自 07-02 后无刷新源（cron-d 停用、sync_product_cache.py 未接线），
新商品缺缓存 → cron-a 处理订单时 cache-miss → halt + P1 卡单（订单 128 实战）。

## 关键实测结论（2026-08-10 生产环境）

| 接口 | 参数 | 返回 | 可用性 |
|------|------|------|--------|
| `erp-goods.goods.sku.search` | lastMaxId 全量 | **仅 skuId**（其余字段全 null） | ❌ 补档案不可用 |
| `erp-goods.goods.sku.search` | goodsNo/barcode 条件 | **仅 skuId** | ❌ 补档案不可用 |
| `erp.stockquantity.get` | goodsNo 过滤 | **完整字段**（41 字段：goodsNo/goodsName/unitName/skuBarcode/库存） | ✅ 可用 |

- 07-02 缓存 1376 行 raw_json 字段形态（quantityId/warehouseId/warehouseName/batchList）
  与 `erp.stockquantity.get` 响应完全一致 → 缓存历史填充源就是库存接口
- `erp.stockquantity.get` 成功 = HTTP 200 + `code=200` + `result.data.goodsStockQuantity[]` 非空
- 失败形态：HTTP 400（JKY 间歇故障）/ code=0（业务失败）/ data 空（**零库存商品查不到**）

## 方案（v2 修订）

### 触发点
cron_a.py cache-miss 分支（现 ~199-212 行）：`jky_product_cache` 查无 goodsNo 时。

### 流程
```
cache-miss
  ├─ 编码长度 < 15 = 基础商品
  │    └─ 调 erp.stockquantity.get {goodsNo, pageIndex:0, pageSize:5}
  │         ├─ 成功（code=200 + data 非空）
  │         │    ├─ goodsNo 精确唯一匹配 → UPSERT 缓存（含 unitName/skuBarcode）→ 继续创单
  │         │    └─ goodsNo 不匹配/多条异编码 → 异常 halt + P1（不写缓存）
  │         ├─ data 空（零库存）→ 异常 halt + P1 等人工补录（fail-closed）
  │         └─ 网络/HTTP 错误 → 有界重试 2 次（间隔 5s）→ 仍失败 halt + P1
  └─ 编码 ≥ 15 = 组合装
       ├─ sku_mapping is_fit=1 → unit 兜底"套" → 继续创单（现有 72df777 逻辑）
       └─ 无 is_fit 记录 → halt + P1 等人工补录
```

### 各 H 项闭合

| # | Codex 发现 | 闭合方案 |
|---|-----------|---------|
| H1 | 恢复闭环缺失（halt 后 audited 不再重拉） | **手动重置 SOP**：人工补完缓存后 `UPDATE order_map SET state='init' WHERE id=?`，下一轮 cron-a 自动重拉（用户拍板，改动最小） |
| H2 | 基础商品 unitName 缺失兜底值不明确 | **仍 halt + P1**（用户拍板：不兜底"件"，避免重蹈 123/124 覆辙） |
| H3 | 成功码契约冲突 | **实测闭合**：点查用 `erp.stockquantity.get`，成功 = code=200 + data 非空；http≠200/code=0/data 空均 halt |
| H4 | 精确匹配必须唯一 | 点查返回多条时校验 goodsNo 全部等于目标编码；任一不同 → 异常 halt |
| H5 | UPSERT 原子写防并发覆盖 | 单条 `INSERT ... ON CONFLICT(jky_goods_no) DO UPDATE`，同事务 |
| H6 | 异常分类 | 网络/HTTP 错误 → 有界重试 2 次（5s 间隔）→ halt；业务失败（code≠200）→ 直接 halt；data 空 → halt；全部 raise P1 |
| H7 | 单商品点查不复用 cron-d 全量 | 点查用 `erp.stockquantity.get` + goodsNo 过滤（pageSize=5），**不碰** lastMaxId 全量逻辑 |

### 改动文件
- `lanmeng_bridge/cron/cron_a.py` — cache-miss 分支（仅此一个文件）
- 不动：sync_product_cache.py（保留给 cron-d 恢复）、cron_d.py、admin.py

### 不改的影响
- 新商品缺缓存 → 订单永久卡在 audited + P1 告警，需人工逐单处理
- 组合装已由 72df777 兜底（不阻塞）；基础商品是当前唯一缺口

## 验证计划
1. 本地单测：mock stock.get 各分支（成功/零库存/网络错/编码不匹配）
2. 部署后重跑订单 128 同款新商品场景（若存在）或构造测试单
3. 三端一致：LOCAL==GITHUB==ECS md5 + health 200 + 4 crons
