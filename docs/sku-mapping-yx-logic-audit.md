# SKU 编码映射（YX 前缀转换）— 审计与修改方案

## 背景

蓝盟正式网站已有 2 个商品的编码以 `YX` 开头，与吉客云的商品编码（`180` 开头）不一致，需要通过 `sku_mapping` 表转换：

| 蓝盟 productNo | 吉客云 goodsNo | 条码 |
|---|---|---|
| YX2SSe7nQFVa4 | 1802025032701 | 6973694372668 |
| YX2SSe7nR6IyG | 1802025110501 | 6973694373160 |

**转换规则**：`productNo` 以 `YX` 开头 → 查 `sku_mapping` 表得到 `jky_goods_no` → 以此值走后续逻辑；否则直接用 `productNo`。

当前 ECS 所有代码都未实现此转换，直接拿 `productNo` 当 `goodsNo` 用。

---

## 受影响的代码位置

### 1. `cron/cron_a.py` L152-L178 — 推单时 SKU 编码转换 ⚠️ 优先级最高

**现代码路径**：

```
product_no = item.get("productNo", "")                           # L152
→ SELECT FROM jky_product_cache WHERE jky_goods_no = ?          # L160 — 直接用 product_no 当 jky_goods_no 查缓
→ "goodsNo": product_no,                                        # L178 — 直接用 product_no 当 goodsNo 传 JKY
```

**需要改**：

```
product_no = item.get("productNo", "")
↓
# 新增：YX 前缀转换
if product_no.startswith("YX"):
    resolved = sku_resolver.resolve(product_no)  # 查 sku_mapping 表
    if not resolved:
        → transition(skipped, f"{product_no} 无sku映射")
        → skip_order
    jky_goods_no = resolved
else:
    jky_goods_no = product_no
# 后续查缓存和传 goodsNo 都用 jky_goods_no
```

**需引入**：`from ..core.sku_resolver import SkuResolver`

**当前状态**：`cron_a.py` 未 import `SkuResolver`。全局 `sku_resolver` 在 `app.py` L125 初始化，但 cron 是独立运行的，需要自己实例化或通过参数传入。

---

### 2. `admin.py` L1229-L1245 — 后台重新提交时 SKU 转换 ⚠️ 优先级高

**现代码路径**：

```python
product_no = item.get("productNo", "")                           # L1229
→ SELECT FROM jky_product_cache WHERE jky_goods_no = ?          # L1234 — 直接用 product_no 查
→ "goodsNo": product_no,                                        # L1245 — 直接用 product_no 传 JKY
```

**需要改**：同 `cron_a.py`，插入 YX 前缀判断 + `sku_resolver.resolve()`。

**当前状态**：`admin.py` 未 import `SkuResolver`。admin 路由在 `app.py` 中上下文可访问全局 `sku_resolver`，但需要确认是否通过依赖注入传入。

---

### 3. `cron/cron_f.py` L257-L262 — 对账商品匹配 ⚠️ 优先级中等

**现代码路径**：

```python
# Build indexes by productNo/goodsNo
lm_by_no = {}
for p in lm_products:
    no = _safe_str(p.get("productNo"))         # L257 — 蓝盟侧 productNo
    if no:
        lm_by_no[no] = p

jky_by_no = {}
for g in jky_goods:
    no = _safe_str(g.get("goodsNo"))            # L262 — JKY 侧 goodsNo
    if no:
        jky_by_no[no] = g
```

**问题**：如果蓝盟订单的 `productNo` 是 `YX2SSe7nQFVa4`，JKY 侧的 `goodsNo` 是 `1802025032701`，则 `lm_by_no` 的 key 是 `YX2SSe7nQFVa4`，`jky_by_no` 的 key 是 `1802025032701`，索引不匹配 → 商品详情对账失效。

**需要改**：构建 `lm_by_no` 索引时，将 YX 开头的 `productNo` 转换为 JKY 的 `goodsNo` 再作为 key：

```python
for p in lm_products:
    no = _safe_str(p.get("productNo"))
    if no and no.startswith("YX"):
        jky_no = sku_resolver.resolve(no)
        if jky_no:
            no = jky_no  # 用转换后的 jky_goods_no 作为匹配 key
    if no:
        lm_by_no[no] = p
```

**关于匹配的双向性**：
- 蓝盟侧 `productNo` → 转换为 JKY `goodsNo` 做索引 key ✓（已覆盖主要场景）
- 如果未来 JKY 侧出现非标准编码需要反查，再补充反向转换
- 当前 2 条映射是正向（YX → 180），只需正向转换

**当前状态**：`cron_f.py` 未 import `SkuResolver`。

---

### 4. `cron/cron_b.py` L201-L214 — 物流回传部分发货商品过滤 ⚠️ 优先级较低（v2 文档功能但未实现）

**v2 文档要求**："部分发货: 按 goodsDetail[].goodsNo 匹配 orderProducts[].productNo, 只回传匹配到的"

**现代码**：ECS 上直接取 `order_items_json` 全量，无 goodsNo/productNo 匹配过滤。

```
order_products = json.loads(row.get("order_items_json") or "[]")   # L201
for prod in order_products:
    ...  # 全量追加 items，无过滤
```

**需要改**（与 YX 映射相关部分）：

```python
# 新增：从 JKY 拉取的 goodsDetail 中获取本次发货的商品 goodsNo 列表
jky_goods = trade.get("goodsDetail") or []
jky_goods_nos = set()
for g in jky_goods:
    gno = _safe_str(g.get("goodsNo"))
    if gno:
        jky_goods_nos.add(gno)

# 对蓝盟侧 productNo 做 YX 转换后再匹配
order_products = json.loads(row.get("order_items_json") or "[]")
for prod in order_products:
    product_no = prod.get("productNo", "")
    if product_no.startswith("YX"):
        resolved = sku_resolver.resolve(product_no)
        matched = resolved in jky_goods_nos if resolved else True  # 未映射的 YX 编码默认不过滤
    else:
        matched = product_no in jky_goods_nos

    if not matched:
        continue  # 部分发货过滤：本商品不在本次发货单中
    # ... 后续 items 构造
```

**当前状态**：此逻辑（部分发货过滤本身）ECS 尚未实现，与 YX 映射是一个独立的并行需求。但 YX 映射会同时影响此处。

---

### 5. DB — `sku_mapping` 表缺数据 ⚠️ 必须先执行

**当前状态**：
- 表存在（`sku_mapping`）✓
- 内容为空（0 行）❌

**需要执行**：

```sql
INSERT INTO sku_mapping (platform_sku_no, jky_goods_no, platform_barcode)
VALUES
    ('YX2SSe7nQFVa4', '1802025032701', '6973694372668'),
    ('YX2SSe7nR6IyG', '1802025110501', '6973694373160');
```

---

## 修改优先级排序

| 优先级 | 位置 | 影响 | 改动量 |
|---|---|---|---|
| P0 | DB INSERT 2 条映射 | 必须最先完成，否则所有 YX 编码查不到映射 | 1 条 SQL |
| P1 | `cron_a.py` SKU 转换 | 正式网站推单直接失败（YX 编码作为 goodsNo 传 JKY 找不到货品） | ~10 行 |
| P1 | `admin.py` SKU 转换 | 后台重新提交同路径 | ~10 行 |
| P2 | `cron_f.py` 索引构建 | 对账时 YX 编码商品匹配不到 JKY 侧，导致假报警 | ~5 行 |
| P3 | `cron_b.py` 部分发货过滤 | 目前 ECS 未实现部分发货过滤功能（独立假设计） | 待后续 |

---

## 风险/注意点

1. **SkuResolver 的初始化**：cron 文件是独立函数调用，需要自己实例化 `SkuResolver()`（其内部调用 `get_connection()` 无额外依赖）。非要从 `app.py` 传全局实例也行，但改动更大。

2. **jky_product_cache 用 jky_goods_no 做 key**：当前 cache 的 `jky_goods_no` 是 `1802025032701` 这样的值。如果直接拿 `YX2SSe7nQFVa4` 去查，必然查不到。这也意味着：YX 编码转换必须在**查 cache 之前**完成。

3. **对账误判风险**：当前对账若绕过（现有 2 条映射的空 DB 场景下对账会报"蓝盟商品在 JKY 不存在"），会导致人工排查成本。插入映射数据 + 对账侧正确匹配可消除此误报。

4. **code=0 还是 code=200 判定混淆**：JKY `trade_create` 成功返回 `code=200`（不是 `code=0`），已在 `5828dde` 修复。确保新改代码不重新引入混淆。
