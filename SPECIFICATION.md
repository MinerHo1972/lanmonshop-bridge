# Lanmonshop-Bridge 完整技术规格书

**Project**: 蓝盟商城(中台) ↔ 吉客云 订单中转桥
**Version**: 0.3.6
**Port**: 18433
**Service URL**: https://bridge.minerho1972.ccwu.cc

---

## 1. 配置系统 (`config.py` + `config/settings.yaml` + `credentials.yaml`)

### 1.1 配置加载链
```
settings.yaml (lhm_admin/projects/.../config/settings.yaml)
  + credentials.yaml (~/.hermes/data/credentials.yaml)
  = merged dict
```

### 1.2 settings.yaml 字段
```yaml
service:
  host: "0.0.0.0"
  port: 18433
  name: "lanmonshop-bridge"

db:
  path: "~/.hermes/data/lanmonshop-bridge.db"   # 展开 ~/ 到 $HOME

lanmong:
  base_url: "https://test-zt-api.lanmonshop.com"
  # appKey/appSecret → credentials.credentials.lanmenshop

jky:
  gateway_url: "http://localhost:18433"
  # api_key → credentials.jky_gateway

cron:
  a_interval_minutes: 60    # 新单同步
  b_interval_minutes: 60    # 状态同步+物流回传兜底
  c_interval_minutes: 15    # 取消检测
  f_hour: 3                  # 对账
  f_minute: 30

feishu:
  p0_at_all: true
  # webhook_url → credentials.feishu.webhook_url

retry:
  max_attempts: 3
  backoff_minutes: [1, 5, 15]

auto_review: true            # 是否自动过审蓝盟订单
```

### 1.3 credentials.yaml 结构
```yaml
credentials:
  lanmenshop:
    appkey: "..."
    secret: "..."
jky_gateway:
  api_key: "..."
  app_secret: "..."          # webhook 验签用
jky_direct:
  appkey: "83311133"         # 默认 fallback
  app_secret: "..."          # 默认 fallback
feishu:
  webhook_url: "..."
```

### 1.4 关键函数
- `load_settings() -> dict`: 加载 settings.yaml + 注入 credentials → " _credentials" 键
- `load_credentials() -> dict`: 读取 credentials.yaml

---

## 2. 数据库 (`storage/db.py`)

### 2.1 连接管理
- **DB Path**: `~/.hermes/data/lanmonshop-bridge.db` (可被 `LANMONSHOP_DB_PATH` 环境变量覆盖)
- **PRAGMA**: `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`
- **连接池**: 单例 per path (全局 _connections dict)
- `get_connection(db_path=None) -> Connection`: 获取连接
- `init_db(db_path=None)`: 创建 schema(幂等) + 运行增量迁移
- `close_all()`: 关闭所有连接
- `log_api_call(source, method, request_body, response_body, http_status, api_code, api_sub_code, error, duration_ms)`: 写入 API 日志
- `get_cursor(cursor_key, default="") -> str`: 读游标
- `set_cursor(cursor_key, cursor_value)`: 写游标(UPSERT)

### 2.2 表 Schema (9 张表)

#### order_map (订单映射主表)
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTO | |
| platform_order_no | TEXT UNIQUE NOT NULL | 蓝盟 orderNo |
| platform_order_id | INTEGER | 蓝盟 orderId |
| platform_state | INTEGER | 蓝盟原始 state |
| jky_trade_no | TEXT | 吉客云 tradeNo |
| logistic_no | TEXT | 物流单号 |
| state | TEXT DEFAULT 'init' | 状态机当前态 |
| retry_count | INTEGER DEFAULT 0 | 重试计数 |
| last_error | TEXT | 最近错误 |
| last_attempt_at | TIMESTAMP | 最近状态变更 |
| closed_at | TIMESTAMP | 异常关闭时间 |
| closed_by | TEXT | 关闭人 |
| closed_note | TEXT | 关闭说明 |
| order_items_json | TEXT | 订单商品 JSON |
| jky_state | TEXT | JKY 原始 tradeStatus |
| platform_unified | TEXT | 蓝盟统一态 |
| jky_unified | TEXT | JKY 统一态 |
| bridge_unified | TEXT | Bridge 统一态 |
| created_at | TIMESTAMP DEFAULT CURRENT_TIMESTAMP | |
| updated_at | TIMESTAMP DEFAULT CURRENT_TIMESTAMP | |

索引: idx_order_map_state, idx_order_map_updated, idx_order_map_platform_state

#### order_status_log (状态审计)
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTO | |
| order_map_id | INTEGER FK | |
| from_state | TEXT | |
| to_state | TEXT NOT NULL | |
| source | TEXT NOT NULL | cron_a/b/c/manual/webhook |
| error | TEXT | |
| ts | TIMESTAMP DEFAULT CURRENT_TIMESTAMP | |

#### sku_mapping (SKU 映射)
| 列 | 类型 |
|---|---|
| platform_sku_no | TEXT PK |
| platform_barcode | TEXT |
| jky_goods_no | TEXT NOT NULL |

#### jky_product_cache (吉客云货品缓存)
| 列 | 类型 | 说明 |
|---|---|---|
| jky_goods_no | TEXT PK | |
| jky_goods_name | TEXT | |
| jky_barcode | TEXT | |
| jky_category | TEXT | "饮料" / "周边" |
| jky_category_id | TEXT | |
| jky_price | REAL | |
| jky_stock | INTEGER | |
| raw_json | TEXT | |
| fetched_at | TIMESTAMP | |

索引: idx_jky_product_barcode, idx_jky_product_category, idx_jky_product_fetched

#### jky_logistic_cache (物流公司缓存)
| 列 | 类型 |
|---|---|
| jky_logistic_no | TEXT PK |
| jky_logistic_name | TEXT |
| raw_json | TEXT |
| fetched_at | TIMESTAMP |

#### jky_product_cache_changes (货品变更审计)
| 列 | 类型 |
|---|---|
| change_id | INTEGER PK AUTO |
| jky_goods_no | TEXT NOT NULL |
| change_type | TEXT | INSERT/DELETE/UPDATE |
| old_value | TEXT | JSON |
| new_value | TEXT | JSON |
| cron_run_id | TEXT | |

#### jky_logistic_cache_changes (物流变更审计)
同 jky_product_cache_changes 结构

#### alert_counter (P2→P1 升级滑动窗口)
| 列 | 类型 |
|---|---|
| exception_class | TEXT PK |
| window_start_ts | INTEGER NOT NULL |
| count | INTEGER DEFAULT 0 |
| last_error | TEXT |
| upgraded_to_p1_at | TIMESTAMP |

#### api_call_log (API 调用日志)
| 列 | 类型 |
|---|---|
| id | INTEGER PK AUTO |
| source | TEXT | lanmong / jky_gateway / jky_direct |
| method | TEXT |
| request_body | TEXT |
| response_body | TEXT |
| http_status | INTEGER |
| api_code | INTEGER |
| api_sub_code | TEXT |
| error | TEXT |
| duration_ms | INTEGER |
| created_at | TIMESTAMP DEFAULT CURRENT_TIMESTAMP |

#### cron_cursor (Cron 游标)
| 列 | 类型 |
|---|---|
| cursor_key | TEXT PK |
| cursor_value | TEXT NOT NULL |
| updated_at | TIMESTAMP |

#### order_merge (合并/拆分追踪)
| 列 | 类型 |
|---|---|
| id | INTEGER PK AUTO |
| source_trade_no | TEXT NOT NULL |
| source_online_trade_no | TEXT |
| merge_type | TEXT | merge / split |
| target_trade_no | TEXT |
| target_online_trade_no | TEXT |
| jky_status | INTEGER |
| order_map_id | INTEGER FK |
| detected_at | TIMESTAMP |

#### reconciliation_report (对账报告)
| 列 | 类型 |
|---|---|
| id | INTEGER PK AUTO |
| report_date | TEXT | YYYY-MM-DD |
| run_id | TEXT NOT NULL |
| summary_json | TEXT NOT NULL |
| deviations_json | TEXT |
| daily_trend_json | TEXT |
| created_at | TIMESTAMP |

### 2.3 增量迁移 (_MIGRATIONS)
幂等 ALTER TABLE: jky_category, order_items_json, jky_state, platform_unified, jky_unified, bridge_unified, order_merge 补充列

---

## 3. 状态机 (`core/state_machine.py`)

### 3.1 状态定义

| 常量 | 值 | 说明 |
|---|---|---|
| STATE_INIT | "init" | 初始态(刚发现) |
| STATE_AUDITED | "audited" | 已过审 |
| STATE_JKY_CREATED | "jky_created" | JKY 已创单 |
| STATE_JKY_SHIPPED | "jky_shipped" | JKY 已发货 |
| STATE_SYNCED | "synced" | 物流已回传蓝盟 |
| STATE_DONE | "done" | 闭环终态 |
| STATE_FAILED | "failed" | 失败 |
| STATE_SKIPPED | "skipped" | SKU缺失跳过 |
| STATE_JKY_CANCELLED | "jky_cancelled" | 蓝盟退→JKY取消 |
| STATE_CANCELLED | "cancelled" | 蓝盟异常不进主流程 |

### 3.2 允许的转移
```
init → [audited, skipped, cancelled, failed]
audited → [jky_created, failed]
jky_created → [jky_shipped, failed, jky_cancelled, done]
jky_shipped → [synced, failed]
synced → [done, failed]
done → []                          (终态)
failed → [init, audited, jky_created]  (重试恢复)
skipped → []                       (终态)
jky_cancelled → []                 (终态)
cancelled → []                     (终态)
```

### 3.3 核心函数
- `is_terminal(state) -> bool`: 是否终态
- `can_transition(from_state, to_state) -> bool`: 检查合法性
- `transition(order_map_id, to_state, source, error=None) -> bool`:
  1. 读当前 state
  2. 检查 can_transition
  3. 原子 UPDATE order_map (state, last_attempt_at, last_error, updated_at)
  4. INSERT order_status_log
  5. conn.commit()
  6. return True/False

### 3.4 状态机流转图
```
蓝盟新单 → init → audited → jky_created → jky_shipped → synced → done [闭环]
                    ↓             ↓             ↓
                 skipped      failed        failed
                 cancelled    jky_cancelled
                 failed
```

---

## 4. 统一状态映射 (`core/shared_unified.py`)

### 4.1 五态统一模型
"待发货", "部分发货", "已发货", "已完成", "已取消/退款"

### 4.2 映射函数

#### platform_to_unified(state) → str
蓝盟 state → 统一态:
- 1→"待发货", 2→"待发货"
- 3→"部分发货"
- 4→"已发货"
- 6→"已完成"
- -2,-3,-4→"已取消/退款"
- None/unknown→"待发货"

#### jky_to_unified(trade_status) → str
JKY tradeStatus → 统一态:
- 1010,1020,1030,1050,2000,2010,2020,2030,2040,4040,4041,4110,4111,4112,4113→"待发货"
- 4130→"部分发货"
- 3010,6000→"已发货"
- 9090→"已完成"
- 4121,4122,4123,5010,5020,5030→"已取消/退款"
- None/unknown→"待发货"

#### bridge_to_unified(state) → str
Bridge state → 统一态:
- init, audited, jky_created, failed, skipped, static→"待发货"
- jky_shipped→"已发货"
- synced, done→"已完成"
- cancelled, jky_cancelled→"已取消/退款"

#### find_split_merge_successor(trade, platform_order_no, all_jky_trades) → str
检测拆合单后继：
- 若 trade 的 tradeStatus==5020, 检查 all_jky_trades 中有无后继单 onlineTradeNo 包含本单号
- 有→返回后继单 tradeStatus; 无→返回 ""

---

## 5. API 客户端

### 5.1 蓝盟 (`clients/lanmonshop.py`)

**鉴权**: `sign = MD5(appKey & timestamp & appSecret).toUpperCase()`
- timestamp = 秒级(非毫秒)
- Header: appKey, sign, timestamp, Content-Type: application/json

**Endpoint**: `POST {base_url}/open/v1/order/...`

**方法**:

| 方法 | 路径 | 参数 | 响应 |
|---|---|---|---|
| `get_deliver_orders(page_num, page_size, time_start/end, pay_time_start/end, supplier_update_time_start/end, order_no, state)` | `/getDeliverOrders` | state: "1"=待审核, "2"=待发货, "4"=已发货, "-2"=已取消 | `{code, data: {orderList: [...], total}}` |
| `review_order(order_no, result=0, reason=None)` | `/reviewOrder` | body: `{list: [{orderNo, result, reason?}]}` | `{code, msg}` |
| `sync_order_express(order_id, order_no, express_no, express_code, express_name, warehouse_id, warehouse_name, items)` | `/syncOrderExpress` | body: `{list: [{orderId, orderNo, expressNo, expressCode, expressName, warehouseId, warehouseName, orderItems: [{orderItemId, num}]}]}` | `{code, data: {faultList}}` |
| `close()` | | | |

**工厂**: `create_lanmong_client(settings) -> LanmongClient`  
日志记录: 每次调用写入 api_call_log (source="lanmong")

### 5.2 吉客云网关 (`clients/jky.py`)

**路由**: 通过 hermes-web-api 网关代理 (localhost:8088)  
**鉴权**: URL query param `api_key`

**方法**:

| 方法 | 路径 | 说明 |
|---|---|---|
| `trade_create(biz)` | POST /jky/trade/create | 创建销售单 |
| `trade_audit(biz)` | POST /jky/trade/audit | 审核 |
| `trade_cancel(biz)` | POST /jky/trade/cancel | 取消 (biz: `{tradeNos, cancelReason}`) |
| `trade_list(biz)` | POST /jky/trade/list | 查询列表 |
| `goods_search(biz)` | POST /jky/goods/list | 搜索货品 |
| `logistic_list(biz)` | POST /jky/logistic/list | 物流公司列表 |

**工厂**: `create_jky_client(settings) -> JkyClient`  
日志记录: source="jky_gateway"

### 5.3 吉客云直连 (`clients/jky_direct.py`)

**Endpoint**: `https://open.jackyun.com/open/openapi/do`  
**鉴权**: `sign = MD5(appSecret + concat_sorted_kv + appSecret).lower()`

**参数构建**: method, appkey, version="v1.0", contenttype="json", timestamp, bizcontent(JSON), sign

**方法**:

| 方法 | JKY method | 说明 |
|---|---|---|
| `trade_create(trade_order)` | oms.trade.ordercreate | body: `{tradeOrder: ...}` |
| `trade_audit(trade_nos, operator)` | oms.trade.audit.pass | body: `{tradeNos: [str], operator}` |
| `trade_cancel(trade_nos, cancel_reason)` | oms.trade.ordercancel | body: `{tradeNos: str, cancelReason}` |
| `trade_list(biz)` | oms.trade.fullinfoget.customized | 默认 fields 含 tradeNo,onlineTradeNo,tradeStatus,... |
| `goods_search(biz)` | erp-goods.goods.sku.search | |
| `logistic_list(biz)` | erp.logistic.get | |

**工厂**: `create_jky_direct_client(settings) -> JkyDirectClient`  
日志记录: source="jky_direct"

---

## 6. Cron 任务

### 6.1 Cron-A: 新单同步 (每60分钟)

**目的**: 蓝盟 → JKY 创单

**流程**:
1. **拉蓝盟**: `get_deliver_orders(state="1,2", supplier_update_time_start=cutoff(15d), page_size=200)` 全量分页 → `all_lanmong: {orderNo: order}`
2. **对比DB刷新**: `SELECT ... FROM order_map WHERE updated_at >= cutoff` → 对蓝盟已有单刷新 platform_unified, 新单 INSERT OR IGNORE
3. **处理新单**: 查询 `WHERE state=init AND platform_order_no IN (...)` 过滤已插入的 init 态
4. **跳过异常**: 蓝盟 state ∈ (-2, -3, -4) → transition → cancelled, continue
5. **自动过审**: `review_order(order_no)` → 成功则 transition → audited; 失败→ transition → failed, continue
6. **SKU解析**: 遍历 orderProducts, 查询 `jky_product_cache WHERE jky_goods_no=?` → 缺缓存则 transition → skipped, continue
7. **JKY创单**: build tradeOrder JSON → `jky.trade_create(biz)` → 成功则 UPDATE order_map (jky_trade_no, order_items_json, jky_state="1010", jky_unified, bridge_unified) + transition → jky_created; 失败则重置 + transition → failed
8. **更新游标**: `set_cursor("cron_a_last_pull", now_str)`

**游标**: key="cron_a_last_pull", 非必用(基于更新时间)

### 6.2 Cron-B: 状态同步+物流回传 (每60分钟)

**目的**: JKY → 蓝盟 物流回传兜底

**流程**:
1. **拉JKY全量**: `trade_list(scrollId, pageSize=200, startModified=7d, shopIds="2154377951944409856", fields)` scroll 分页
2. **构建拆合单索引**: 对于 onlineTradeNo 含逗号(多个原单号)且非5020的单, 映射 successor_index
3. **查DB**: `SELECT ... FROM order_map WHERE jky_trade_no IS NOT NULL AND updated_at >= cutoff(15d)`
4. **逐单比对**:
   - 查拆合单后继: `find_split_merge_successor(trade, platform_order_no, all_jky)` → effective_ts
   - 计算 jky_unified, bridge_unified
   - 若有变化: `UPDATE order_map SET jky_state, jky_unified, bridge_unified`
5. **物流回传**: 仅当 `new_jky_unified in {"已发货","已完成"}` 且 `postid` 非空且 `state not in (done, synced)`:
   - transition → jky_shipped (若当前是 jky_created/failed)
   - `logistic_resolver.resolve(logist_name)` → platform_code/name
   - 解析 items 从 order_items_json
   - 重试循环 (RetryState max=3, backoff=[1,5,15]min): `sync_order_express(...)`
   - 成功: UPDATE logistic_no, transition → synced → done
   - 失败: UPDATE retry_count, last_error, transition → failed, `notifier.alert_p1(...)`

### 6.3 Cron-C: 取消检测 (每15分钟)

**目的**: 三端取消/退款对比兜底

**流程**:
1. **拉蓝盟已取消**: `get_deliver_orders(state="-2,-3,-4", 15d)` → `lanmong_cancelled: {orderNo: state}`
2. **拉JKY全量**: 同 cron-b, 加拆合单识别 (5020检查后继)
3. **筛选JKY已取消**: tradeStatus ∈ {5010,5020,5030,4122}, 排除拆合单 5020 → `jky_cancelled_ts`
4. **查DB**: `SELECT ... FROM order_map WHERE updated_at >= cutoff(15d)`
5. **蓝盟取消→JKY未取消→调JKY cancel**:
   - 每单: `jky.trade_cancel({"tradeNos": jky_trade_no, "cancelReason": "420001"})`
   - 成功: UPDATE jky_unified, bridge_unified, platform_unified + transition → jky_cancelled
6. **JKY取消→蓝盟正常→P0告警**: `notifier.alert_p0(...)`

### 6.4 Cron-D: 货品列表拉取 (每天 02:00)

**目的**: 刷新 jky_product_cache

**流程**:
1. 分页拉取 `goods_search(pageIndex, pageSize=100, category=["饮料","周边"])`
2. 规范化字段 (兼容多种命名)
3. diff 算法: 对比旧 SELECT → to_delete / to_insert / to_check
4. 事务: DELETE + INSERT changes / INSERT + INSERT changes / UPDATE + INSERT changes
5. 失败重试 1 次 → P1 告警

### 6.5 Cron-E: 物流公司列表拉取 (每天 02:30)

同 cron-D 算法, 操作 jky_logistic_cache + jky_logistic_cache_changes

### 6.6 Cron-F: 三方对账 (每天 03:30)

**目的**: 蓝盟/JKY/DB 三端逐单对比

**流程**:
1. 拉蓝盟全量: 所有 state (-4,-3,-2,1,2,3,4,6) 分页, 30天窗口
2. 拉DB: `SELECT ... FROM order_map WHERE updated_at >= cutoff(30d)`
3. 拉JKY全量 scroll 分页, 7天窗口
4. 兜底刷新 DB 统一态: 蓝盟态 + JKY态(含拆合单) → platform_unified, jky_unified, bridge_unified
5. 三端对账 `_build_report()`: 逐单比对 → 偏差判定:
   - 蓝盟已取消但DB未取消
   - DB done但蓝盟未发货
   - DB已发货但JKY状态异常
   - DB synced但JKY缺物流单号
   - DB有但蓝盟未返回
6. 格式化飞书消息 `_format_feishu_report()` → 发送
7. 落库 reconciliation_report

---

## 7. 核心组件

### 7.1 物流解析器 (`core/logistic_resolver.py`)

```python
LogisticResolver(yaml_path=None)
  .resolve(jky_logistic_code: str) -> dict  # {platform_code, platform_name}
  .all_codes -> list[str]                   # 所有映射 key
```
配置源: `config/logistic.yaml` → `logistic_mapping` + `fallback`

### 7.2 SKU 解析器 (`core/sku_resolver.py`)

```python
SkuResolver()
  .resolve(platform_sku_no: str) -> Optional[str]        # → jky_goods_no
  .resolve_by_barcode(barcode: str) -> Optional[tuple]   # → (sku_no, goods_no)
  .is_in_jky_product_cache(barcode: str) -> bool
  .get_missing_type(platform_sku_no, barcode) -> str     # "jky_not_found" | "mapping_missing"
```

### 7.3 异常处理 (`core/exception_handler.py`)

**Severity**: P0(资损), P1(单订单卡住), P2(偶发自动恢复)

**RetryState**:
- max_attempts=3, backoff_minutes=[1,5,15]
- `is_exhausted`, `next_backoff_seconds()`, `record_attempt(error)`

**classify_error(error, retry_state) -> Severity**:
- P0: 货已发/credential泄露/auth fail
- P1: 重试耗尽/SKU缺映射
- P2: timeout/connection/rate limit/429/busy/lock
- 其他: attempt>=2→P1, else P2

**P2UpgradeTracker** (DB-backed via alert_counter):
- 30min滑动窗口, 连续3次P2→升级P1
- 升级后1h cooldown
- `record(exception_class, last_error) -> bool` (是否触发升级)
- `count(exception_class) -> int`
- `is_upgraded(exception_class) -> bool`
- `reset(exception_class)`

### 7.4 飞书通知 (`notify/feishu.py`)

```python
FeishuNotifier(webhook_url, p0_at_all=True)
  ._send(content: str)                     # POST webhook
  .alert_p0(order_no, summary, id, state)  # P0 资损 @all
  .alert_p1(order_no, last_error, retry, id)  # P1 单订单卡住
  .alert_p2_upgrade(error_type, detail, count, affected)  # P2→P1升级
  .close()
```

### 7.5 权限 (`auth.py`)

**飞书 OAuth 登录**:
- APP_ID="cli_a95f4ef526381bc4", APP_SECRET 硬编码
- 回调 URL: `https://bridge.minerho1972.ccwu.cc/admin/auth/callback`
- Session: 24h TTL, in-memory dict, cookie "admin_session", httponly+secure+lax
- 路由: `/admin/auth/login`, `/feishu`, `/callback`, `/logout`

---

## 8. FastAPI 应用 (`app.py`)

### 8.1 生命周期
```
lifespan:
  init_db(path)
  create_lanmong_client, create_jky_client, create_jky_direct_client
  SkuResolver(), LogisticResolver()
  FeishuNotifier(webhook)
  scheduler.add_job(cron_a, interval, minutes=a_interval)
  scheduler.add_job(cron_b, interval, minutes=b_interval)
  scheduler.add_job(cron_c, interval, minutes=c_interval)
  scheduler.add_job(cron_f, cron, hour=f_hour, minute=f_minute)
  yield
  scheduler.shutdown()
  close_all()  # db + all clients
```

Cron 调度参数: max_instances=1, coalesce=True, misfire_grace_time=300

### 8.2 路由

| 路径 | 方法 | 说明 |
|---|---|---|
| `/` | GET | Redirect → /admin |
| `/health` | GET | `{status: "ok"}` |
| `/admin` | GET | Admin dashboard (HTML) |
| `/admin/api/crons` | GET | Cron status |
| `/admin/api/logs` | GET | API 日志查询(分页/筛选) |
| `/admin/api/reconciliation` | GET | 三态对账数据 |
| `/admin/api/reconciliation/pull-lanmong` | POST | 从蓝盟重新拉取 |
| `/admin/api/reconciliation/pull-jky` | POST | 从吉客云重新拉取 |
| `/admin/api/reconciliation/resubmit-lanmong` | POST | 回传蓝盟物流 |
| `/admin/api/reconciliation/resubmit` | POST | 重提(根据状态决定操作) |
| `/admin/api/reconciliation/reports` | GET | 对账日报列表 |
| `/admin/api/reconciliation/reports/{id}` | GET | 日报详情 |
| `/jky/webhook/oms.trade.confirm` | POST | 吉客云发货确认回调 |
| `/jky/trade/create` | POST | 直连: 创建销售单 |
| `/jky/trade/audit` | POST | 直连: 审核 |
| `/jky/trade/cancel` | POST | 直连: 取消 |
| `/jky/trade/list` | POST | 直连: 查询列表 |
| `/jky/goods/list` | POST | 直连: 货品搜索 |
| `/jky/logistic/list` | POST | 直连: 物流列表 |

### 8.3 Webhook 处理
`/jky/webhook/oms.trade.confirm`:
1. 解析参数 (query + body, 兼容 TOP 回调)
2. D-C 验签: MD5(appSecret + concat_sorted_kv + appSecret).lower()
3. 业务幂等: `process_oms_trade_confirm(trade_no, payload)`
   - 查 order_map WHERE jky_trade_no=?
   - 非本桥单 → ack 跳过
   - 已 synced/done → 幂等 ack
   - 更新 logistic_no
   - transition → jky_shipped (若当前是 jky_created/failed/audited)
   - 其他态 → 写日志

---

## 9. Admin 对账页面 (`admin.py`)

### 9.1 前端
SPA 单页: 3 个主 Tab (对账/Cron状态/API日志) + 对账子 Tab (待处理/日报)
- 暗色主题 (GitHub Dark 风格)
- 三端统一态用颜色徽标: 待发货(蓝), 部分发货(黄), 已发货(绿), 已完成(绿), 已取消(红)
- 操作栏: 4种动作 (拉蓝盟/提JKY/拉JKY/回传蓝盟)
- 多选+批量执行

### 9.2 对账 API 端点

#### GET /admin/api/reconciliation
前置: 拉蓝盟近期已取消(state=-2), 刷新 DB platform_state  
返回: 30天窗口, 优先级排序(init/failed→蓝盟取消→jky_created→其他), 限200条  
每条含: platform_unified, bridge_unified, jky_unified, consistent(bool)

#### POST /admin/api/reconciliation/pull-lanmong
参数: `{ids: [int]}`  
逐单: `get_deliver_orders(order_no=X)` → UPDATE platform_state, platform_unified

#### POST /admin/api/reconciliation/pull-jky
参数: `{ids: [int]}`  
逐单: `trade_list({"tradeNos": trade_no})` → 返回 JKY 状态

#### POST /admin/api/reconciliation/resubmit-lanmong
参数: `{ids: [int]}`  
逐单: `sync_order_express(...)` → 回传物流

#### POST /admin/api/reconciliation/resubmit
参数: `{ids: [int]}`  
根据状态决策:
- init/failed & 无 jky_trade_no: 重新创单
- platform_state<0: 调 jky_direct.trade_cancel → jky_cancelled
- jky_created/audited & 有 jky_trade_no: 提示在 JKY 后台手动审核
- jky_shipped/synced: 已流程中

### 9.3 推荐操作逻辑 `_suggest_action()`
1. init/failed & 无JKY单 → resubmit-jky
2. 蓝盟已取消 & bridge未处理 → pull-lanmong
3. jky_created/audited & 有JKY单 → pull-jky
4. done & 蓝盟非已发货 → resubmit-lanmong
5. jky_shipped/synced → resubmit-lanmong
6. 默认 → pull-lanmong

### 9.4 偏差分类 `_classify_drift()`
- terminal: done/skipped/jky_cancelled/cancelled (静态度)
- critical: platform_state<0 (蓝盟取消)
- high: failed / stale_48h
- medium: init / jky_created/audited/jky_shipped/synced / stale_24h
- low: 其他 pending

---

## 10. 错误处理模式 (全局)

- 所有 API 调用包装 try/except → 日志记录 + continue/break
- 重试: `RetryState` 类, while not is_exhausted 循环
- 告警: `FeishuNotifier.alert_p0/p1/p2_upgrade` (fail-silent)
- 数据库: 事务回滚模式用于 diff 操作
- 状态机: 非法转移返回 False, 不抛异常
- Webhook: 验签失败 → 401; 处理异常 → 200 {code:-1} (不阻塞重试)

---

## 11. 关键数据流

### 11.1 正向流程 (新单)
```
蓝盟 getDeliverOrders(state=1,2)
  → 对比 DB platform_unified
  → review_order (自动过审)
  → 查 jky_product_cache (SKU映射)
  → jky.trade_create (JKY创单)
  → UPDATE order_map (jky_trade_no, state=jky_created)
```

### 11.2 发货回传流程
```
JKY webhook oms.trade.confirm
  → 验签 → 幂等检查
  → UPDATE logistic_no
  → transition → jky_shipped

cron-b 兜底(60min):
  → JKY trade_list scroll 全量
  → 查拆合单后继
  → 检测已发货+有物流单号+未闭环
  → sync_order_express (回传蓝盟)
  → transition → synced → done
```

### 11.3 取消流程
```
cron-c(15min):
  → 蓝盟取消单 vs JKY
  → 蓝盟取消但JKY未取消 → jky.trade_cancel
  → JKY取消但蓝盟正常 → P0告警
```

### 11.4 对账流程
```
cron-f(每天03:30):
  → 蓝盟全量(所有state, 30d)
  → DB order_map(30d)
  → JKY全量(7d scroll)
  → 逐单比对 → 偏差检测
  → 飞书日报 + 落库
```
