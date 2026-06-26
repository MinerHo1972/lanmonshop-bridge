# 蓝盟中台 ↔ 吉客云 订单中转桥 PRD

> **项目代号**:`lanmonshop-bridge`(待用户最终拍名)
> **版本**:v0.2(2026-06-25 草案,基于 v0.1 + 异常处理 SOP + 中台取消反向同步)
> **范围**:一期 — 仅"订单下载 + 发货回传"2 个接口 + 异常闭环
> **来源**:基于《中台对外开放接口 0622(T)》PDF + 吉客云 Gateway `jackyun-api` skill 沉淀 + 本对话 4 轮迭代

> 🆕 **v0.2 vs v0.1 增量**:
> - §4.2 schema 加 4 字段:`platform_state` / `closed_at` / `closed_by` / `closed_note`
> - §4.3 状态机加 1 状态:`jky_cancelled`
> - §4.7 API 对照表加 1 路由:`/jky/trade/cancel`
> - §9 新增章节:异常处理 SOP(P0/P1/P2 分级 + 自动处置 + 残留人工 + 飞书告警模板)

---

## 1. 问题陈述

公司作为供应商接入蓝盟商城中台(lanmonshop),中台产生的销售订单需要自动进入吉客云 ERP 走仓库发货;吉客云发货完成后,物流信息需自动回传中台,完成订单闭环。

**当前痛点**(假设):
- 人工从蓝盟中台后台导单 → 手工录入吉客云销售单 → 仓库发货 → 人工回填物流单号
- 频次高 + 人工介入多 + 出错率高 + 不可追溯
- **异常情况无闭环**(v0.2 重点):中台取消/退款/异常时,吉客云销售单无法联动处理,资损风险存在

## 2. 解决方案

**单 Python FastAPI 服务 + APScheduler 3 cron + SQLite 3 表 + 异常 SOP**:

- **cron-a (5min)**:中台 → 吉客云(`getDeliverOrders` 拉单 → 自动过审 → 创吉客云单 + 审核)
- **cron-b (1min)**:吉客云 → 中台(`/jky/trade/list` 监听已发货 → 拿物流单号 → `syncOrderExpress` 回传)
- 🆕 **cron-c (5min)**:对账扫"中台已退但吉客云未退"孤儿单 → 调吉客云 `oms.trade.ordercancel`
- **数据中台化**:3 张表覆盖"订单映射 + 状态审计 + SKU 映射",物流映射走静态 YAML
- **异常告警**:飞书 webhook 实时推送,所有状态变更全审计,异常按 P0/P1/P2 分级处置
- **吉客云发货流程完全不介入**:我们只创建销售单 + 监听状态,仓库作业由吉客云自有流程完成
- 🆕 **异常闭环**:异常关闭写入 `order_map.closed_at/closed_by/closed_note`,审计即架构

## 3. 用户故事(User Stories)

### 正常流程

1. 作为供应商运营,中台产生的"已支付待审核"订单应在 5 分钟内自动出现在吉客云待审核列表,无需我手工录单
2. 作为供应商运营,自动过审应有开关(配置项),以便异常 SKU/异常订单能人工把关
3. 作为供应商运营,每个订单的每个状态变更都有审计日志可查
4. 作为供应商运营,中台订单被取消/退款时,我方吉客云销售单应自动取消(避免资损)
5. 作为供应商运营,SKU 映射缺失时该订单跳过而不阻塞整个拉单任务
6. 作为供应商运营,物流回传失败时自动重试 N 次,超阈值飞书告警(不丢单)
7. 作为供应商运营,中台订单部分发货(state=3)应支持多次回传同一 orderNo
8. 作为供应商运营,系统应自动跳过中台 -2/-3/-4 异常/取消/退款订单(未创单时),不进入主流程

### 异常流程

9. 作为供应商运营,飞书告警应包含:订单号 + 失败原因 + 重试次数 + 时间戳 + order_map.id 用于 SQL 查询
10. 作为供应商运营,同一订单不应同时被 cron-a/b/c 处理(单 worker 串行保证)
11. 作为供应商运营,凭证(appKey/secret)不应硬编码在代码或 commit 中
12. 作为系统管理员,服务异常退出后 systemd 应自动拉起
13. 作为系统管理员,ECS 内存占用应可控,不挤占已有 4 套服务资源
14. 作为业务方,订单状态在中台 ↔ 吉客云两处应保持最终一致(秒级到分钟级延迟可接受)
15. 🆕 作为运营,异常订单飞书告警后我能在 30min 内 mark closed(SQL UPDATE)
16. 🆕 作为运营,异常关闭有审计字段(closed_at/closed_by/closed_note),事后可追溯谁处理的
17. 🆕 作为运营,P0 异常(如吉客云已发货但中台已退)我能分钟级响应,有清晰的诊断信息

### 运维流程

18. 作为系统管理员,DB 异常/磁盘满应有告警,不应静默失败
19. 作为系统管理员,所有 cron 执行结果应有日志 + 耗时 metrics
20. 作为系统管理员,手动重跑某订单全流程应通过 CLI/SQL 直接操作,不依赖 UI
21. 🆕 作为运维,服务进程崩溃后 systemd 自动拉起,启动时从 DB 恢复状态,不丢正在处理中的订单

## 4. 实现决策(Implementation Decisions)

### 4.1 模块边界(目录结构)

```
lanmonshop-bridge/
├── app.py                  # FastAPI 入口 + APScheduler 启动
├── clients/
│   ├── lanmonshop.py       # 中台 API 封装(鉴权 + 4 接口)
│   └── jky.py              # 吉客云 API 封装(走 /jky Gateway)
├── storage/
│   └── db.py               # SQLite 连接 + 3 张表 schema
├── core/
│   ├── state_machine.py    # 订单状态机 + 状态变更审计
│   ├── sku_resolver.py     # SKU 映射查询 + 缺失告警
│   └── logistic_resolver.py# 物流映射(YAML 加载)
├── cron/
│   ├── cron_a.py           # 中台 → 吉客云(5min)
│   ├── cron_b.py           # 吉客云 → 中台(1min)
│   └── cron_c.py           # 🆕 中台已退 → 吉客云取消(5min,对账)
├── notify/
│   └── feishu.py           # 飞书告警 webhook (P0/P1/P2 三套模板)
├── config/
│   ├── settings.yaml       # 凭证/开关/超时(脱敏)
│   └── logistic.yaml       # 物流映射配置
├── tests/
│   └── ...                 # 见 §5 测试决策
└── scripts/
    └── init_sku_mapping.py # SKU 映射初始化工具
```

### 4.2 持久化 Schema(3 表) — 🆕 v0.2 加 4 字段

```sql
-- 订单映射(主表)
CREATE TABLE order_map (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform_order_no TEXT UNIQUE NOT NULL,    -- 中台 orderNo(渠道单号)
  platform_order_id INTEGER,                  -- 中台 orderId(发回传用)
  platform_state INTEGER,                     -- 🆕 v0.2:中台原始 state(1/2/3/4/6/-2/-3/-4),cron-c 对账 key
  jky_trade_no TEXT,                         -- 吉客云销售单号
  logistic_no TEXT,                          -- 物流单号
  state TEXT NOT NULL DEFAULT 'init',        -- 状态机当前态
  retry_count INTEGER DEFAULT 0,             -- 发货回传失败重试计数
  last_error TEXT,                           -- 最近一次错误
  last_attempt_at TIMESTAMP,                 -- 最近一次状态变更时间
  closed_at TIMESTAMP,                       -- 🆕 v0.2:异常关闭时间(异常闭环审计)
  closed_by TEXT,                            -- 🆕 v0.2:异常关闭人(运营姓名)
  closed_note TEXT,                          -- 🆕 v0.2:异常关闭说明(原因)
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_order_map_state ON order_map(state);
CREATE INDEX idx_order_map_updated ON order_map(updated_at);
CREATE INDEX idx_order_map_platform_state ON order_map(platform_state); -- 🆕 v0.2:cron-c 索引

-- 状态变更审计(审计即架构)
CREATE TABLE order_status_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_map_id INTEGER NOT NULL,
  from_state TEXT,                           -- 前态(NULL = 首次)
  to_state TEXT NOT NULL,                    -- 后态
  source TEXT NOT NULL,                      -- cron_a / cron_b / cron_c / manual / api / human_close
  error TEXT,                                -- 错误详情(NULL = 正常)
  ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (order_map_id) REFERENCES order_map(id)
);
CREATE INDEX idx_status_log_order ON order_status_log(order_map_id);
CREATE INDEX idx_status_log_ts ON order_status_log(ts);

-- SKU 映射
CREATE TABLE sku_mapping (
  platform_sku_no TEXT PRIMARY KEY,          -- 中台 skuNo(字符串)
  platform_barcode TEXT,                     -- 中台 69 开头条码(冗余,便于人工核对)
  jky_goods_no TEXT NOT NULL,                -- 吉客云 goodsNo
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_sku_barcode ON sku_mapping(platform_barcode);
```

### 4.3 状态机(6 态) — 🆕 v0.2 加 jky_cancelled

```
init → audited → jky_created → jky_shipped → synced → done
                  ↓              ↓
                  failed ←────── failed(回传失败)
                  ↓
                  jky_cancelled (v0.2 新增)

旁路:
- 中台 -2/-3/-4 → cancelled(不进主流程,仅审计记录)
- SKU 缺映射 → skipped(飞书告警 + 不阻塞)
- cron-c 检测 jky_created + platform_state=-2/-3/-4 → 调 oms.trade.ordercancel → jky_cancelled (v0.2 新增)
```

### 4.4 字段映射表 — 拉单端(中台 → 吉客云)

| 中台字段(中台示例) | → 吉客云字段 | 转换说明 |
|---|---|---|
| `orderId`(843) | `onlineTradeNo` | 中台 ID 作线上单号参考 |
| `orderNo`(S202205201338448623) | `tradeNo` ? | ⚠️ **待 confirm**:吉客云 `oms.trade.ordercreate` 的 `tradeNo` 字段是否可外部传入 |
| `name`(蓝天) | `receiverName` | 直传 |
| `mobile`(18721592908) | `receiverMobile` | 直传 |
| `province`(上海) + `city`(上海市) + `district`(普陀区) + `address`(真北路) | `receiverAddress` | 拼接:`{province}{city}{district}{address}` |
| `orderProducts[].skuNo`(heliceshi001) | `assemblyGoodsDetail[].goodsNo` | **查 sku_mapping 表** → jky_goods_no |
| `orderProducts[].number`(2) | `assemblyGoodsDetail[].qty` | 直传 |
| `orderProducts[].costPrice`(3.00) | — | **跳过**(吉客云 ordercreate 不接供货价) |
| `expressPrice`(0.00) | `expressPrice` | 直传 |
| `remark`(测试开放接口) | `buyerMemo` | 直传 |
| `state`(1/2/3/4/6/-2/-3/-4) | — | **存到 `order_map.platform_state`(v0.2 新增)**,不进吉客云 |

### 4.5 字段映射表 — 发货回传端(吉客云 → 中台)

| 吉客云字段 | → 中台字段 | 转换说明 |
|---|---|---|
| `tradeNo`(吉客云销售单号) | `orderId`(841) | **order_map 反查** platform_order_id |
| `mainPostid`(物流单号) | `expressNo` | 直传 |
| `logisticName`(吉客云物流公司名) | `expressName` | 查 `logistic.yaml`(已配项) |
| `logisticCode`(吉客云物流编码) | `expressCode` | 查 `logistic.yaml` |
| `assemblyGoodsDetail[].skuNo` | `orderItems[].skuNo` | **order_map 反查** 中台 skuNo |
| `assemblyGoodsDetail[].qty` | `orderItems[].num` | 直传 |
| — | `warehouseId` + `warehouseName` | **中台虚拟仓,默认值待 confirm**(中台不关心实际物理仓) |

### 4.6 物流映射 YAML 配置(`config/logistic.yaml`)

```yaml
logistic_mapping:
  - platform_code: "sf"
    platform_name: "顺丰"
    jky_logistic_no: "<待查>"      # 吉客云物流编码(需 verify)
    jky_logistic_name: "顺丰速运"
  - platform_code: "zt"
    platform_name: "中通"
    jky_logistic_no: "<待查>"
    jky_logistic_name: "中通快递"
  # ... 后续按需追加
```

> **不放数据库**:一期物流公司有限,改配置比重启服务成本低。后续接入多平台再上 DB。

### 4.7 API 对照表 — 🆕 v0.2 加 1 路由

| 任务 | 接口 | 鉴权 | 频次 | 调用方 | 数据源 |
|---|---|---|---|---|---|
| 拉单 | 中台 `POST /open/v1/order/getDeliverOrders` | appKey + sign | cron-a 5min | 服务 | state=1 增量 |
| 过审 | 中台 `POST /open/v1/order/reviewOrder` | appKey + sign | cron-a 内部 | 服务 | result=0 |
| 创吉客云单 | JKY `POST /jky/trade/create`(待新增路由) | token | cron-a 内部 | 服务 | `oms.trade.ordercreate` |
| 审吉客云单 | JKY `POST /jky/trade/audit`(待新增路由) | token | cron-a 内部 | 服务 | `oms.trade.audit.pass` |
| 拉吉客云已发货 | JKY `POST /jky/trade/list` | token | cron-b 1min | 服务 | modified_time 增量 + state=已发货 |
| 回传物流 | 中台 `POST /open/v1/order/syncOrderExpress` | appKey + sign | cron-b 内部 | 服务 | 批量 |
| 🆕 取消吉客云单 | JKY `POST /jky/trade/cancel`(待新增路由) | token | cron-c 5min | 服务 | `oms.trade.ordercancel` |
| 仓库列表(辅助) | 中台 `POST /open/v1/warehouse/getList` | appKey + sign | 启动时缓存 | 服务 | 一次性 |
| 物流公司列表(辅助) | 中台 `POST /open/v1/express/getList` | appKey + sign | 启动时缓存 | 服务 | 一次性 |
| 飞书告警 | 自建 webhook | token | 异常时 | 服务 | text 消息(P0/P1/P2 三套模板,见 §9.4) |

### 4.8 关键架构决策理由

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 服务架构 | 单服务 vs 微服务 | **单 FastAPI** | 一期单租户单中台,无分布式需求;减法 |
| 持久化 | SQLite vs PostgreSQL | **SQLite** | 单服务足矣,后续多租户/多中台再迁;减法 |
| 拉单方式 | cron vs webhook | **cron 5min** | 中台 webhook 未确认,不赌;沿用吉客云已验证模式 |
| 吉客云发货触发 | 主动调 API vs 纯监听 | **纯监听** | 用户明确"不关注仓库作业流程";减法 |
| 仓库映射 | 表 vs 不用 | **不用** | 中台虚拟仓 + 吉客云分仓策略接住 |
| SKU 映射 key | skuNo vs barcode | **skuNo** | 中台订单返回无 barcode 字段 |
| 物流映射 | DB vs YAML | **YAML** | 配置项有限,改配置比重启服务轻 |
| 凭证管理 | env vs yaml vs agents.yaml 风格 | **settings.yaml + .env** | 沿用 launch-tracker 模式,不入 commit |
| 飞书发送路径 | curl OpenAPI vs lark-cli | **lark-cli** | SSoT 原则,user 2026-06-06 first-class 纠正:飞书 markdown 默认走 lark-cli |
| 🆕 异常处理策略 | 仅告警 vs 分级闭环 | **P0/P1/P2 分级 + 自动处置 + 残留人工 + 验证** | 审计即架构,异常不能止于信号 |
| 🆕 中台取消处理 | 不处理 vs cron-c 对账 | **cron-c 5min 对账** | 资损风险兜底,不增加中台 webhook 依赖 |

### 4.9 待新增 JKY Gateway 路由(前置依赖)

按 `jackyun-api` skill 沉淀,**新增路由需改 3 文件 + 吉客云开放平台订阅**:

```python
# services/jky.py — 注册权限
{"id": "oms.trade.ordercreate", "label": "创建销售单"},
{"id": "oms.trade.audit.pass", "label": "销售单审核"},
{"id": "oms.trade.ordercancel", "label": "销售单取消"},  # 🆕 v0.2

# main.py — 新增 3 个路由(参考 /jky/trade/list 风格)
@app.post("/jky/trade/create")
@app.post("/jky/trade/audit")
@app.post("/jky/trade/cancel")  # 🆕 v0.2

# 重启 + 验证
sudo systemctl restart hermes-web-api
```

> ⚠️ 此外需要在吉客云开放平台(open.jackyun.com)订阅这 3 个 method,否则返回 `subCode: 0130020310`。
> ⚠️ **实施前必须 verify `oms.trade.ordercancel` 的适用范围**:未审核/待审核/已发货/已签收 各状态能否取消?已发货/已签收的取消可能要售后流程,不在我们接口范围。

## 5. 测试决策

### 5.1 测试标准

- **只测外部行为**:API 调用 + DB 状态变更 + 飞书告警文本
- **不测内部实现**:state_machine 内部跳转逻辑通过端到端覆盖

### 5.2 测试边界(优先用已有 seam)

| 层级 | 范围 | 用例 |
|---|---|---|
| 单元 | `clients/lanmonshop.py` 鉴权签名 + 参数组装 | mock HTTP,断言 sign 计算正确 |
| 单元 | `clients/jky.py` 调用 `/jky/*` 路由 | mock HTTP,断言透传 |
| 单元 | `core/state_machine.py` 6 态跳转 | mock DB,断言 state 字段 + audit log |
| 单元 | `core/sku_resolver.py` 缺映射返回 None | 测 hit/miss 两条路径 |
| 单元 | 🆕 `core/exception_handler.py` P0/P1/P2 分级判定 | mock 异常输入,断言分级 |
| 集成 | cron-a 完整跑通(中台 → 吉客云) | 临时 SQLite,中台 mock,断言 3 表状态正确 |
| 集成 | cron-b 完整跑通(吉客云 → 中台) | 同上 |
| 集成 | 🆕 cron-c 完整跑通(中台 -2 → 吉客云 cancel) | 同上 |
| 端到端 | 1 个 toy 订单全流程 | 蓝盟测试环境 + 吉客云沙盒 |
| 端到端 | 🆕 异常路径:模拟 syncOrderExpress 失败 3 次 → 飞书 P1 告警 → SQL mark closed → audit log 记录 | 飞书 webhook 验证 |
| 端到端 | 🆕 异常路径:模拟中台 -2 在 cron-a 创单后 → cron-c 检测 → 调吉客云 cancel → state=jky_cancelled | 同上 |

### 5.3 端到端真验 SOP(部署前必跑)

1. 蓝盟测试环境创建 1 个 toy 订单(state=已支付待审核)
2. 启服务 → 验证 cron-a/b/c 都注册成功
3. 验证:中台订单 state=2(已自动过审)
4. 验证:吉客云沙盒有对应销售单(待审核)
5. 仓库人员手工递交到仓库 → WMS 发货(模拟)
6. 验证:cron-b 1min 内监听到已发货
7. 验证:中台订单 state=4(已发货)+ expressNo 已填
8. 飞书验证:收到 5 条状态变更审计消息(init → audited → jky_created → jky_shipped → synced)
9. 🆕 异常路径:模拟 SKU 缺映射 → 验证该单跳过 + 飞书 P1 告警 + SQL mark closed → closed_at 写入
10. 🆕 异常路径:模拟中台 -2 在 cron-a 创单后 → cron-c 5min 内触发 → 吉客云 cancel 成功 → state=jky_cancelled

## 6. 不包含的范围(Out of Scope)

1. **库存同步** — 中台库存 vs 吉客云库存 不做双向同步,接受超卖风险
2. **售后** — 中台退款/退货/换货不接入,仅识别跳过(-2/-3/-4)
3. **商品上传** — `POST /open/v1/product/bulkImport` 不实现(独立功能)
4. **商品上下架 / 修改库存** — 独立功能,本期不做
5. **手动重跑订单 UI** — 不做 UI,直接 SQL/CLI 操作
6. **多中台账号 / 多吉客云店铺** — 单租户单 appKey
7. **TLS 自签 / 加密传输** — 一期明文 HTTP/HTTPS,后续再加
8. **GUI 看板** — 一期只飞书告警 + DB 直查
9. **吉客云发货主动触发** — `oms.trade.ordercompleteDelivery` / `wms-ods.order.basecreate` 不用
10. **物流轨迹查询** — 一期只回传发货节点,不做签收节点
11. 🆕 **飞书回复自动化 mark closed** — 一期 SQL UPDATE,二期再做飞书 webhook 捕获回复
12. 🆕 **on-call 自动派单** — 一期飞书群告警,人工接管;二期按异常级别自动 @特定人

## 7. 补充说明

### 7.1 关键外部依赖(需用户推进)

| # | 依赖项 | 提供方 | 状态 |
|---|---|---|---|
| 1 | 蓝盟 appKey + appSecret | 蓝盟对接人 | 待申请 |
| 2 | 吉客云物流公司编码清单 | 吉客云文档 / JKY OTS | 待查 |
| 3 | 吉客云 OTS 已订阅 `oms.trade.ordercreate` + `oms.trade.audit.pass` + `oms.trade.ordercancel` | 开放平台 | 待 verify |
| 4 | 仓库人员 SOP:吉客云销售单创建后,需手工递交到仓库 | 仓库团队 | 待培训 |
| 5 | ECS 8.153.195.8 内存余量 verify | 运维 | 待跑 `free -h` |
| 6 | SKU 映射初始化数据来源 | 用户 / 系统 | 待定(上传商品响应 vs 商品列表 vs 人工导入) |
| 7 | on-call 名单(P0 / P1 联系人) | 用户 | 待定(本期接受默认运营白班) |
| 8 | `oms.trade.ordercancel` 适用范围 verify | 吉客云文档 / 测试 | 待 verify(已发货/已签收能否取消) |

### 7.2 风险 disclose(必须用户知道)

1. **依赖仓库人员手工递交吉客云销售单给仓库**:若缺失这步,吉客云销售单永远卡"待发货",物流回传不发生。**需 SOP 保障**
2. **中台 API 频次限制未明示**:文档未给 QPS,可能存在 rate limit;待蓝盟 confirm。**应对**:cron-a 单次批量拉取,失败 backoff
3. **中台 SKU 编码和吉客云 goodsNo 体系不一致**:sku_mapping 表初始化需要一次性数据准备工作,可能需要 1-2 天人工核对
4. **最终一致性**:中台/吉客云两系统状态不会实时同步,秒级到分钟级延迟,业务可接受;不做分布式事务
5. **凭证管理**:appKey/secret 写入 `settings.yaml` + `.env`,**.gitignore 必须包含,不入 commit**
6. 🆕 **P0 三方不一致阈值 = 5 单**:阈值偏高,可能漏掉小规模但严重的异常(如 1 单吉客云已发但中台已退,价值可能很高)。**需后续根据真实业务调整阈值**
7. 🆕 **cron-c 5min 延迟边界**:中台取消到吉客云单取消最多 5-10min。如果用户在 5min 内就催单/客服介入,可能看到吉客云"还在"。业务已确认可接受
8. 🆕 **mark closed 一期走 SQL UPDATE**:不强制走飞书回复,运营需手工执行 1 行 SQL(留有培训成本)

### 7.3 命名(待 user 拍)

- 项目名候选:`lanmonshop-bridge` / `order-relay` / `lanmonshop-orders` / 其他
- 数据库:`/home/lhs_admin/.hermes/data/lanmonshop-bridge.db`
- systemd service:`lanmonshop-bridge.service`
- 部署路径:`/opt/lanmonshop-bridge/`

---

## 8. 下一步动作清单

按依赖顺序:

- [ ] **Step 0**:ECS 8.153.195.8 内存 verify + 4 套服务资源占用基线
- [ ] **Step 1**:用户申请蓝盟测试环境 appKey + appSecret
- [ ] **Step 2**:JKY Gateway 新增 3 个路由 `/jky/trade/create` + `/jky/trade/audit` + `/jky/trade/cancel`(沿用 `/jky/trade/list` 风格)
- [ ] **Step 3**:吉客云开放平台订阅 `oms.trade.ordercreate` + `oms.trade.audit.pass` + `oms.trade.ordercancel`
- [ ] **Step 4**:服务骨架 init(FastAPI + SQLite + 3 cron + state_machine + exception_handler)
- [ ] **Step 5**:SKU 映射初始化(决策点待 user 拍)
- [ ] **Step 6**:物流映射 YAML 填充(吉客云物流编码待查)
- [ ] **Step 7**:端到端真验(§5.3 SOP,含 2 条新异常路径)
- [ ] **Step 8**:部署 systemd + verify + 飞书告警联调
- [ ] **Step 9**:灰度观察 1 周 → 全量
- [ ] 🆕 **Step 10**:运营培训(SQL UPDATE mark closed + 飞书 P1 告警模板识别)

---

## 9. 异常处理 SOP — 🆕 v0.2 新增

### 9.1 异常分级标准

| 分级 | 触发条件 | 响应 | 备注 |
|---|---|---|---|
| **P0** | 吉客云已发货但中台已退(cron-c 失败 + 货已发) | 立即人工 | 资损风险最高 |
| **P0** | 服务进程连续 restart 失败 ≥ 3 次 | 立即人工 | 整链不可用 |
| **P0** | 三方状态严重不一致(中台/吉客云/DB 偏差 ≥ 5 单) | 立即人工 | 系统级异常 |
| **P0** | 凭证泄露 | 立即人工 | 安全事件 |
| **P1** | `syncOrderExpress` 失败 ≥ 3 次 | 自动重试 + 残留人工 30min | 单订单卡住 |
| **P1** | SKU 缺映射 | 自动跳过 + 残留人工 30min | 单订单卡住 |
| **P1** | `cron-c` 调吉客云 `ordercancel` 失败 | 自动重试 + 残留人工 30min | 单订单卡住 |
| **P2** | 网络超时 | 自动重试,不告警 | 偶发 |
| **P2** | 吉客云 API rate limit | 自动重试,不告警 | 偶发 |
| **P2** | DB 偶发锁 | 自动重试,不告警 | 偶发 |
| **P2 → P1** | 同异常类型 30min 内连续 3 次 | 升级 P1 | 累计升级 |

### 9.2 自动处置规则

**业务调用**(`syncOrderExpress` / `cron-c` cancel / `cron-a` 创单等):

```
重试次数:   N=3
backoff:    1min → 5min → 15min (指数)
API 超时:   connect=5s + read=20s (沿用 jackyun-api 标准)
累计失败:   state=failed + retry_count=3 + 飞书 P1 告警
```

**对账 / 感知类**(`cron-c` / health check):

```
重试次数:   N=2
backoff:    固定 5min
失败:       飞书告警(立即,不背 backoff 累)
```

**P2 → P1 升级判定**:

```
同一异常类型(以 exception class 区分):
  30min 滑动窗口内连续 3 次触发
    → 升级 P1
    → 飞书群告警带累计次数
    → 不再降回 P2(直到异常类型消失 1 小时)
```

### 9.3 残留人工 SOP

**谁处理**:

```
P0: on-call 运营 / 运维(白班;夜间紧急升级)
P1: 运营(白班)
P2: 无需人工介入
```

**SLA**:

```
P0: 立即(分钟级响应)
P1: 30min 内 mark closed
P2: 不计入 SLA
```

**mark closed 方式**(一期):

```sql
-- 一期:运营直接 SQL UPDATE
UPDATE order_map SET
  closed_at = CURRENT_TIMESTAMP,
  closed_by = 'your_name',
  closed_note = '处理说明(简短,如"手动调中台 API 同步")'
WHERE id = {id_from_feishu_alert};

-- audit log 自动记录(source='human_close')
-- 二期:飞书消息回复"已处理" + webhook 自动捕获 → 自动 SQL
```

**验证闭环**:

```
- closed_at 写入 = 自动视为已闭环
- order_status_log 自动追加一条(source='human_close', to_state='closed')
- 飞书告警消息自动 mark resolved(可选,二期)
- 异常关闭后,该订单不再被 cron 处理(除非手动 reopen)
```

### 9.4 飞书告警消息模板

**P0 模板**(最简 + @人):

```
🚨 [P0 资损风险]
单号: {order_no}
现象: {one_line_summary}
订单: order_map.id={id}
当前状态: {state}
操作: 立即查 order_map → 决定手动干预
```

**P1 模板**(含诊断 + SQL):

```
⚠️ [P1 单订单卡住]
单号: {order_no}
失败原因: {last_error}
已重试: {retry_count}/3
订单: order_map.id={id}
SQL: SELECT * FROM order_map WHERE id={id};
状态日志: SELECT * FROM order_status_log WHERE order_map_id={id} ORDER BY ts DESC LIMIT 10;
```

**P2 → P1 升级模板**(只升级时发一次):

```
🔁 [P2→P1 升级] {异常类型}
时段: {time_range}
累计次数: {count}/3
详情: {last_error}
影响范围: {affected_orders_count} 单
```

**审计字段写入样例**:

```
-- 异常关闭时 audit log 自动追加
INSERT INTO order_status_log (order_map_id, from_state, to_state, source, error)
VALUES ({id}, 'failed', 'closed', 'human_close', '{closed_note}');
```

---

**End of PRD v0.2**

---

## 11. v0.3 增量（2026-06-26 拍板）

> 本节记录 2026-06-26 飞书 DM 4 项拍板 + 对 scope 计划的影响。
> PRD v0.2 主体（§1-§9）保持不变；v0.3 仅追加本节，不回填覆盖。

### 11.1 4 项拍板记录

| 编号 | 拍板项 | 拍板选择 | 替代选项 | 决策依据 |
|---|---|---|---|---|
| P1 | scene block 增量策略 | A = 不动 scene block | B = replace_all / C = 先修 bug 再 patch | scene block 自身有 21 行重复段，patch 工具 unique anchor 不可达；design doc 落文件系统已审计可追溯，下次 session 起的 system prompt 通过 project dir read 可发现 → 减法原则穿透 |
| P2 | JKY Gateway 改造方案 | A = 追加 3 路由到现有 hermes-web-api | B = 独立部署新 bridge gateway / C = JKY Gateway 作为 api-hub provider | 已生产验证方案优先（hermes-web-api 已托管 /jky/* 全路由），不破坏 launch-tracker 链路；零迁移成本；不引入未验证同质替代品（减法三层穿透） |
| P3 | 项目名最终拍名 | 蓝萌API对接项目（中文） + lanmonshop-bridge（英文 slug） | PRD §7.3 候选 lanmonshop-bridge / order-relay / lanmeng-api / 其他 | 中文名锁定业务实体 + 英文 slug 满足代码 / filesystem / Python import / systemd service / DB 路径 / git 仓 6 处统一；"蓝萌"是"蓝盟"在 PRD 落地期间的口语简称（user 拍板 2026-06-26）|
| P4 | SKU 映射初始化数据来源 | 吉客云 API 每日拉取货品列表存数据库表（每日刷新） | 人工导入 CSV / 商品上传响应 / 中台 getProductList | 中台 skuNo 体系在 jky 商品库一定有映射（业务上 SKU 一定是先在吉客云登记再上中台卖）；每日刷新保证新 SKU 不漏；自动化为默认，人工为审计事件；cron-d 失败 = 飞书 P1 告警 |

### 11.2 SKU 映射数据源扩展（v0.2 → v0.3 新增 cron-d）

#### 11.2.1 现状 vs 拍板后

- v0.2 现状：SKU 映射初始化数据来源待定（PRD §7.1 #6），scripts/init_sku_mapping.py = bootstrap 一次性
- v0.3 拍板：每日 02:00 自动从吉客云拉取货品列表，存数据库表 jky_product_cache；sku_mapping 表改为 cron-d 每日刷新（非一次性 bootstrap）

#### 11.2.2 新增表 jky_product_cache（吉客云货品列表 cache）— Schema-Auditor 一等公民

```sql
CREATE TABLE jky_product_cache (
  jky_goods_no TEXT PRIMARY KEY,        -- 吉客云 goodsNo（SKU 映射 jky 端 SSoT）
  jky_goods_name TEXT,                  -- 吉客云商品名（人工核对用）
  jky_barcode TEXT,                     -- 吉客云条码（69 开头，与 PRD §4.4 69 条码桥对齐）
  jky_category_id TEXT,                 -- 吉客云分类 ID（备用，scope 4 可选启用）
  jky_price REAL,                       -- 吉客云售价（一期不入吉客云 ordercreate，跳过；保留以备二期）
  jky_stock INTEGER,                    -- 吉客云库存（一期不入吉客云 ordercreate，跳过；保留以备二期）
  raw_json TEXT,                        -- 原始 API 响应（审计即架构，Schema-Auditor）
  fetched_at TIMESTAMP,                 -- 拉取时间（cron-d 监控用）
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_jky_product_barcode ON jky_product_cache(jky_barcode);
CREATE INDEX idx_jky_product_fetched ON jky_product_cache(fetched_at);
```

#### 11.2.3 新增 cron-d（每日 02:00 CST）

```
cron-d: 吉客云货品列表每日拉取 + 刷 sku_mapping
  频次:    1/day @ 02:00 CST（流量低谷，避 00:00-01:00 日切）
  API:     JKY POST /jky/goods/list（scope 3 新增路由 = D1 实施工作量 +1）
  method:  待 verify = oms.goods.query（吉客云 OTS 订阅 +1）
  数据源:  jky_product_cache 全量刷新（TRUNCATE + INSERT in transaction）
          sku_mapping 表增量同步（新增 jky_goods_no + 检测已下架 SKU）
  失败:    自动重试 1 次（5min 后）→ 仍失败 → 飞书 P1 告警
  成功:    不告警（heartbeat 写 log 即可）
  影响:    SKU 映射 jky 端 SSoT = jky_product_cache.jky_goods_no
          业务 cron-a 用 sku_mapping 时如发现 jky_goods_no 已下架 → 标 stale + 飞书 P2 告警
```

#### 11.2.4 scripts/init_sku_mapping.py 调整

- 原 v0.2：bootstrap 一次性
- v0.3：bootstrap（首次） + 日常不再人工维护，sku_mapping 由 cron-d 增量同步
- 异常路径：cron-d 失败期间，人工可执行 init_sku_mapping.py 应急同步

#### 11.2.5 SKU 缺映射语义重定义

- 原 v0.2：SKU 缺映射 = sku_mapping 表无对应 platform_sku_no → 跳过 + 飞书 P1
- v0.3：SKU 缺映射 = 两种可能：
  - (A) jky 商品库没有 → 需先在吉客云建品 → 飞书 P1 告警 + 提示"先在吉客云建品"
  - (B) jky 有但中台未推 → 飞书 P1 告警 + 提示"中台推 SKU"
- 区分依据：cron-d 拉的 jky_product_cache 中是否含此 platform_barcode

### 11.3 D1=A 影响（JKY Gateway 改造方案 A 拆解）

#### 11.3.1 方案 A 范围

追加 4 路由到现有 hermes-web-api（v0.2 候选 3 路由 + v0.3 cron-d 需要 1 路由）：

```
/jky/trade/create     (oms.trade.ordercreate)
/jky/trade/audit      (oms.trade.audit.pass)
/jky/trade/cancel     (oms.trade.ordercancel, v0.2 新增)
/jky/goods/list       (oms.goods.query, v0.3 cron-d 需要)
```

#### 11.3.2 实施步骤

1. 改 services/jky.py 注册权限 +1 项（oms.goods.query）
2. 改 main.py 加 1 路由（/jky/goods/list）
3. 重启 hermes-web-api.service
4. curl 4 路由验证无 subCode 错误

#### 11.3.3 风险

- 重启 hermes-web-api 短暂影响 launch-tracker 等依赖服务（秒级到分钟级）
- 不迁出 /jky/* 路由 = launch-tracker 链路零变更
- systemd NotifyAccess=main 验证热重启不中断 in-flight 请求（scope 3 verify 项）

### 11.4 scope 计划调整（v0.2 → v0.3）

| Scope | v0.2 范围 | v0.3 调整 |
|---|---|---|
| scope 0 | ECS 内存 + 4 服务资源 verify | 不变 |
| scope 1 | 蓝盟凭据 + jky OTS 3 method + 物流编码 + ordercancel 范围 | + oms.goods.query 订阅 verify（cron-d 需要）|
| scope 2 | 脚手架（FastAPI + 3 cron + SQLite + state_machine + 异常 SOP）| + 4 cron 占位（cron-d 新增）|
| scope 3 | 3 路由改造 + 中台 client + SKU bootstrap + YAML | 4 路由改造（D1=A，+1 = goods/list）+ 中台 client + SKU 改 cron-d + YAML |
| scope 4 | 3 cron 业务逻辑 | 4 cron 业务逻辑（cron-d 实施 sku_mapping 增量同步逻辑）|
| scope 5 | 端到端真验 SOP | + cron-d 验证步骤（拉取后 sku_mapping 完整性）|
| scope 6 | 灰度 + 培训 | + cron-d 失败应急 SOP（人工 init_sku_mapping.py 路径培训）|

### 11.5 项目名工程化映射（Schema-Auditor 一等公民）

中文名: 蓝萌API对接项目
英文 slug: `lanmonshop-bridge`（**1A 拍板沿用 v0.2 候选**，user 2026-06-26）
- 决策依据：仓名稳定（v0.2 期间已沿用）+ 仓目录物理路径 `~/projects/lanmonshop-bridge/` 不需 rename + 中文 == 英文在 §11.5 表格已标注映射（"蓝萌"为"蓝盟"口语简称，与"蓝盟"业务实体系同一实体，仓目录沿用 PRD v0.2 候选 `lanmonshop-bridge` 保留溯源一致性）

映射表（中文 == 英文 == 仓名 == systemd service == DB 路径 == 飞书群 == git 仓）：

| 维度 | 值 |
|---|---|
| 中文项目名 | 蓝萌API对接项目 |
| 英文 slug | lanmonshop-bridge |
| 本地仓路径 | `~/projects/lanmonshop-bridge/`（如重命名则 mv）|
| Python 包名 | `lanmeng_bridge`（import 路径）|
| systemd service | `lanmonshop-bridge.service` |
| DB 路径 | `~/.hermes/data/lanmonshop-bridge.db` |
| 飞书群 | "蓝萌API对接项目"（中文）|
| GitHub 仓（待建）| `MinerHo1972/lanmonshop-bridge` |
| 飞书 PRD docx | https://sekqrm4f9b.feishu.cn/docx/W6OpdmoOVorBFdx3RKgcCBn4nhd（v0.2 URL；v0.3 增量同步走 lark-cli）|

### 11.6 scene block 同步策略（1A 决定）

- 不动 scene block `技术运维-OpenAgents基础设施.md`（1A 拍板）
- 理由：scene block 自身有 21 行重复段（数据冗余 bug），patch 工具 unique anchor 不可达
- 取证路径：下次 session 起的 system prompt 注入时，project dir `~/projects/lanmonshop-bridge/` read 可发现 docs/future-api-hub.md + 本节 §11 = 完整设计上下文

### 11.7 触发条件（来自 docs/future-api-hub.md §4）

任一条件满足时重新评估 api-hub 抽象：
- 第 3 个 API 接入需求出现（不含 jky + 中台）
- 现有 provider 间出现 ≥ 3 处可复用代码片段
- 跨 agent 共享 api-hub 需求出现
- 任意 provider 鉴权/调用代码出现安全事件

---

**End of PRD v0.3**