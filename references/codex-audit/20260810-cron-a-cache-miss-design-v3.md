# cron-a cache-miss 自恢复设计（修订版 v3.1）

> 状态: 待步骤④ 实现（六步流程步骤③ 第二轮审计完成，REJECT 项全部闭合）
> 日期: 2026-08-10
> 前置: v2 审计 REJECT（4 High）用户拍板 → v3 审计 REJECT（新增 High + Medium 全部为实现细化）→ v3.1 闭合

## 用户决策（不可更改）

| # | Codex High 项 | 用户决策 |
|---|--------------|---------|
| H1 | 组合装 cache-miss 分支不可达 + goodsName 无受信任来源 | **B：组合装 cache-miss 一律 halt + P1 等人工补录，不自动创单**（最保守，无需 goodsName 来源） |
| H2 | init 重驱重复过审 | 保留手动 `UPDATE state='init'` 重驱，但 cron-a 过审逻辑须修复：蓝盟 state=2 仅本地 `init→audited`，**绝不重复调 review_order** |
| H3 | JkyClient 无 stock 方法 | 改动范围扩为 4 文件：jky_direct.py + app.py + jky.py + cron_a.py |
| H4 | pageSize=5 无法证唯一 | 点查强制带 `warehouseCode="02"`（创单固定仓）+ 唯一匹配校验 |
| H5 | is_fit OR+fetchone 隐患 | is_fit 查询改精确 `EXISTS` 语义，不匹配/多条 → halt |

## 已实测结论（生产环境，不可推翻）

- `erp-goods.goods.sku.search`（全量/条件）现在**只返回 skuId** → 补档案不可用
- `erp.stockquantity.get` 按 goodsNo 过滤返回**完整字段**（goodsNo/goodsName/unitName/skuBarcode，41 字段）→ ✅ 点查数据源
- 成功 = HTTP 200 + `code=200` + `result.data.goodsStockQuantity[]` 非空
- 失败形态：HTTP 400（JKY 间歇）/ code=0（业务失败）/ data 空（零库存）
- 07-02 缓存 1376 行 raw_json 字段形态与 stock.get 响应一致 → 历史填充源就是库存接口
- 创单固定 `warehouseCode="02"`（cron_a.py:310）

## 流程（v3）

```
cache-miss (prod_row 不存在)
  ├─ 编码 ≥15 = 组合装 → halt + P1 等人工补录（用户 B 决策）
  └─ 编码 <15 = 基础商品 → 调 erp.stockquantity.get {goodsNo, warehouseCode:"02", pageIndex:0, pageSize:5}
       ├─ 成功 code=200 + goodsStockQuantity 非空
       │    ├─ 恰好 1 条且 goodsNo == 目标 → UPSERT 缓存（含 unitName/skuBarcode/raw_json）→ 继续创单
       │    └─ 0 条 / 多条 / goodsNo ≠ 目标 → halt + P1（不写缓存）
       ├─ code=0（业务失败）→ halt + P1（不重试）
       └─ 网络/HTTP 错误（连接超时/408/429/5xx）→ 重试 2 次（5s 间隔，共 3 次）→ 仍失败 halt + P1
            未知 HTTP 400（非瞬态签名）→ 直接 halt + P1（告警含 status/code/body/attempt）
```

## 字段映射（写缓存）

| 缓存列 | 来源 | 校验 |
|--------|------|------|
| jky_goods_no | `str(record["goodsNo"]).strip()` | 必须 == 目标编码 |
| jky_goods_name | `record["goodsName"].strip()` | 非空 |
| jky_barcode | `record["skuBarcode"].strip()` | 非空（**不是** barcode/barCode） |
| raw_json | `json.dumps(完整单条库存 record)` | 必须含 unitName + skuName（创单代码读取） |
| fetched_at | CURRENT_TIMESTAMP | — |

任一项校验失败 → 不写缓存、halt + P1。

## 恢复闭环（人工 SOP，v3.1 修正）

1. 收到 P1 → 查 `order_map.last_error` 定位商品编码
2. 运营从 JKY 主数据/UI 核实 goodsNo/goodsName/skuBarcode/unitName
3. **条件式事务恢复**（一条 SQL 完成校验+更新，禁止裸 REPLACE）：
   ```sql
   INSERT INTO jky_product_cache (jky_goods_no, jky_goods_name, jky_barcode, raw_json, fetched_at)
   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
   ON CONFLICT(jky_goods_no) DO UPDATE SET
     jky_goods_name=excluded.jky_goods_name, jky_barcode=excluded.jky_barcode,
     raw_json=excluded.raw_json, fetched_at=excluded.fetched_at;
   -- 然后条件更新（affected rows 必须 == 1，否则拒绝）：
   UPDATE order_map SET state='init', last_error=NULL, updated_at=CURRENT_TIMESTAMP
   WHERE id=? AND state='audited' AND jky_trade_no IS NULL;
   ```
4. 同事务插入 `order_status_log`（操作人/工单号写入 error JSON）
5. 校验 affected rows==1 才提交；否则回滚 + 保留 P1（防误重放已创单订单）
6. 下一轮 cron-a 自动重拉：基础商品走缓存；组合装同样走缓存（人工补录后存在）

## 点查后的数据承接（v3.1 新增 High 闭合）

UPSERT 成功后**必须重新 `SELECT` 完整 prod_row**（jky_barcode/jky_goods_name/raw_json），
不得直接沿用点查 record 或旧的 None 值。重新读取后校验 jky_goods_name/barcode 非空才进入创单；
任一项仍缺失 → halt + P1（不创单）。

## 组合装 raw_json 空兜底（v3.1 Medium 闭合）

组合装缓存行存在但 raw_json 空（unitName 缺失）时：
- 兜底"套"**仅当** `EXISTS(SELECT 1 FROM sku_mapping WHERE jky_goods_no=? AND is_fit=1)` 精确命中才允许
- 同时记录专项告警（非静默）：提示该组合装缓存缺档案，建议人工补全 raw_json
- 基础商品 raw_json 缺失仍 halt + P1（不变）

## 改动文件

| 文件 | 改动 |
|------|------|
| `clients/jky_direct.py` | + `stockquantity_get(biz)` → `_call("erp.stockquantity.get", biz)` |
| `app.py` | + `POST /jky/stock/query` 路由（StockQueryBody → jky_direct） |
| `clients/jky.py` | + `stockquantity_get(biz)` → `_post("/jky/stock/query", biz)` |
| `cron/cron_a.py` | ① cache-miss 分支：组合装 halt / 基础商品点查+UPSERT ② 过审逻辑：蓝盟 state=2 仅本地转 audited ③ is_fit 查询改精确 EXISTS |

## 验证计划

1. 本地 mock 单测（10+ 分支）：成功写缓存并创单 / data 空 / code=0 / 未知 400 / 超时 3 次 / 编码不匹配 / 多条候选 / unitName 缺失 / 组合装 cache-miss halt / 人工 init 重驱不重复过审
2. 部署后三端一致：LOCAL==GITHUB==ECS md5 + health 200 + 4 crons
3. 生产验证：确认 `/jky/stock/query` 路由权限 + envelope `result.data.goodsStockQuantity`
