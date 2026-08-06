# lanmonshop-bridge 管理后台订单处理 + 告警整合方案

> 状态: v0.2 · Codex audit reviewed · 2026-07-05

## 1. 背景

告警方案（v0.3）覆盖了 cron 和 admin 的关键失败路径，但存在两个缺口：
1. **管理后台的订单处理操作本身不触发告警**——admin 重提失败目前只返回 UI 错误，没有飞书通知
2. **告警与后台之间没有闭环**——运营收到告警后，缺乏"从这里点进去处理"的指引；后台也看不到某单是否曾触发告警

本方案将管理后台订单处理能力与告警体系整合为完整工作流。

## 2. 当前 Admin 订单处理能力（现状）

### 2.1 路由结构

| 路由 | 方法 | 功能 |
|------|------|------|
| `/admin/api/reconciliation` | GET | 订单对账表（分页 + 排序 + 30天窗口） |
| `/admin/api/reconciliation/pull-lanmong` | POST | 手动拉取蓝盟近期订单刷新 platform_state |
| `/admin/api/reconciliation/pull-jky` | POST | 手动拉取 JKY 近期订单刷新 jky_state |
| `/admin/api/reconciliation/resubmit` | POST | 根据状态机重新提交订单到 JKY |
| `/admin/api/reconciliation/resubmit-lanmong` | POST | 回传物流到蓝盟（syncOrderExpress） |
| `/admin/api/reconciliation/reports` | GET | 列出对账日报 |
| `/admin/api/reconciliation/reports/{id}` | GET | 日报详情 |

### 2.2 订单表字段（/api/reconciliation 返回）

```
id, platform_order_no, platform_state, jky_trade_no, logistic_no,
state, retry_count, last_error, updated_at,
platform_unified, jky_unified, jky_effective_unified, bridge_unified, jky_state
```

### 2.3 状态机路线（resubmit）

```
platform_state < 0 → JKY 取消失败 → cancel 重试 → 失败 P1
state init/failed → 重新创单 → 失败 P1
state jky_created/audited → 已有 JKY 单 → 通知用户（不告警）
state jky_shipped/synced → 在流程中 → 不操作（不告警）
state done/jky_cancelled/cancelled → 终态跳过（不告警）
```

## 3. 整合方案

### 3.1 新增 Alert 相关表结构

在 `order_map` 表新增字段（通过 migration）：

```sql
ALTER TABLE order_map ADD COLUMN alert_count INTEGER DEFAULT 0;
ALTER TABLE order_map ADD COLUMN last_alert_level TEXT DEFAULT '';
ALTER TABLE order_map ADD COLUMN last_alert_time TEXT DEFAULT '';
ALTER TABLE order_map ADD COLUMN last_alert_message TEXT DEFAULT '';
```

新增 `alert_log` 表：

```sql
CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    platform_order_no TEXT,
    level TEXT NOT NULL,            -- 'P0', 'P1', 'P2'
    category TEXT NOT NULL,         -- 如 'create_fail', 'cancel_fail', 'resubmit_fail'
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX idx_alert_log_order ON alert_log(order_id);
CREATE INDEX idx_alert_log_created ON alert_log(created_at);

-- 清理策略：P0/P1 保留 180 天，P2 保留 90 天
-- 在 cron 每日执行或启动时检查
```

### 3.2 核心辅助函数：`record_admin_alert(...)`

所有 admin 操作失败路径统一调用此函数，**不各自重复写** alert_log + notifier + order_map 三件套：

```python
def record_admin_alert(conn, notifier, order_id: int, platform_order_no: str,
                       level: str, category: str, message: str,
                       resolved: bool = False):
    """事务性记录 admin 操作告警

    - 脱敏：消息截断 200 字，不含手机号/地址/收件人
    - 写 alert_log + 更新 order_map 聚合字段
    - 调用 notifier
    - 如果 resolved=True，清零该订单的告警计数（修复成功后调用）
    """
    safe_msg = message[:200]  # 截断脱敏

    if resolved:
        # 修复成功 → 清零告警计数
        conn.execute(
            "UPDATE order_map SET alert_count = 0, last_alert_level = '', "
            "last_alert_time = '', last_alert_message = '' WHERE id = ?",
            (order_id,)
        )
        conn.commit()
        return {"recorded": False, "reason": "resolved"}

    if level == "P0":
        notifier.alert_p0(f"Admin {category}", f"order={platform_order_no}, {safe_msg}")
    elif level == "P1":
        notifier.alert_p1(f"Admin {category}",
                          f"order={platform_order_no}, {safe_msg}")
    elif level == "P2":
        notifier.alert_p2(f"Admin {category}",
                          f"order={platform_order_no}, {safe_msg}",
                          category=category)

    conn.execute(
        "INSERT INTO alert_log (order_id, platform_order_no, level, category, message) "
        "VALUES (?, ?, ?, ?, ?)",
        (order_id, platform_order_no, level, category, safe_msg)
    )
    conn.execute(
        "UPDATE order_map SET alert_count = alert_count + 1, "
        "last_alert_level = ?, last_alert_time = strftime('%Y-%m-%d %H:%M:%S','now'), "
        "last_alert_message = ? WHERE id = ?",
        (level, safe_msg, order_id)
    )
    conn.commit()
    return {"recorded": True, "level": level}
```

### 3.3 告警与 Admin 操作映射矩阵

| Admin 操作 | 失败条件 | 等级 | 消息内容 | 备注 |
|---|---|---|---|---|
| resubmit → cancel JKY | JKY cancel 返回非 200 | P1 | order_no, JKY msg | |
| resubmit → create JKY | 蓝盟拉单失败 | P1 | order_no, exception | |
| resubmit → create JKY | SKU 缺映射 | P1 | order_no, productNo | 选中订单无法创单 → 运营需立即干预 |
| resubmit → create JKY | 货品缓存/条码为空 | P1 | order_no, goodsNo | 阻止创单 → 运营需补缓存 |
| resubmit → create JKY | JKY 创单返回非 200 | P1 | order_no, JKY code+msg | |
| resubmit → create JKY | 成功响应但缺 tradeNo | P0 | order_no, @all | 资损风险 |
| resubmit → create JKY | 客户端不可用 | P1 | jky/lanmong | 配置/服务问题 |
| resubmit-lanmong | 蓝盟 syncOrderExpress 失败 | P1 | order_no, 蓝盟 msg | |
| resubmit-lanmong | 物流回传局部失败 (faultList) | P2 | order_no, fault msg | |
| pull-lanmong | 蓝盟 API 异常 | P2 | exception | pull 为非关键刷新 |
| pull-jky | JKY API 异常 | P2 | exception | pull 为非关键刷新 |

### 3.4 Admin UI 新增：告警状态列

在订单对账表新增列：

```javascript
// 列定义
<th>🚨 告警</th>

// 渲染
function alertBadge(row) {
  if (!row.alert_count) return '-';
  const cls = row.last_alert_level === 'P0' ? 'badge-err' : 'badge-warn';
  return `<span class="badge ${cls}" onclick="showAlertLog(${row.id})" style="cursor:pointer">
    ${row.last_alert_level} ×${row.alert_count}</span>`;
}
```

### 3.5 Admin UI 新增：订单告警弹窗

```javascript
function showAlertLog(orderId) {
  fetch(`/admin/api/alerts?order_id=${orderId}`)
    .then(r => r.json())
    .then(data => {
      document.getElementById('modal-body').textContent =
        data.alerts.map(a => `[${a.created_at}] ${a.level} ${a.category}: ${a.message}`).join('\n');
      document.getElementById('body-modal').classList.add('show');
    });
}
```

### 3.6 API 端点：告警查询（复用飞书 OAuth）

```python
@router.get("/api/alerts")
async def api_alerts(request: Request, order_id: int = None,
                     level: str = None, limit: int = 50):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    conn = get_connection()
    where = []  # 动态构建 WHERE
    params = []
    if order_id:
        where.append("order_id = ?"); params.append(order_id)
    if level:
        where.append("level = ?"); params.append(level)
    where_clause = " WHERE " + " AND ".join(where) if where else ""
    sql = f"SELECT * FROM alert_log{where_clause} ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return {"alerts": [dict(r) for r in rows]}
```

### 3.7 告警 → Admin 处理闭环流程

```
飞书 P1 告警 ──→ 运营打开 Admin
  │                  │
  ▼                  ▼
  "待处理" tab   找到该订单
  (异常优先排序)   (alert_count > 0 红色标识)
  │                  │
  ▼                  ▼
  点击告警徽标 ──→ 弹窗显示该订单完整告警历史
  │
  ▼
  勾选订单 → 点击"执行"
  │
  ├── 成功 → record_admin_alert(resolved=True) 清零计数
  └── 失败 → record_admin_alert(P1) 飞书通知下一个值班人员
```

### 3.8 对账日报增强

日报 `summary_json` 中嵌入告警统计（不新增独立列）：

```json
{
  "alert_summary": {
    "p0_count": 0,
    "p1_count": 5,
    "p2_count": 3,
    "top_categories": [
      {"category": "create_fail", "count": 3},
      {"category": "cancel_fail", "count": 2}
    ]
  }
}
```

### 3.9 alert_log 清理策略

```python
# 在启动时或 cron 中执行
def cleanup_alert_log(conn):
    """P0/P1 保留 180 天，P2 保留 90 天"""
    cutoff_180 = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_90 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "DELETE FROM alert_log WHERE created_at < ? AND level IN ('P0','P1')",
        (cutoff_180,)
    )
    conn.execute(
        "DELETE FROM alert_log WHERE created_at < ? AND level = 'P2'",
        (cutoff_90,)
    )
    conn.commit()
```

## 4. 实现范围

### Phase 1: DB Migration（先执行）
1. `ALTER TABLE order_map` 新增 4 个告警相关列
2. `CREATE TABLE alert_log` + 索引
3. 在 `app.py` 启动时调用 `cleanup_alert_log` 做清理

### Phase 2: notifier 扩展
1. 给 `FeishuNotifier` 加上 `alert_p2` 方法（已在告警方案 v0.3 定义）

### Phase 3: API 层
1. 实现 `record_admin_alert()` 辅助函数
2. `admin.py` — 新增 `GET /api/alerts` 端点（复用飞书 OAuth）
3. `admin.py` — `_resubmit_one` / `_resubmit_create` / `resubmit-lanmong` 失败路径调用 `record_admin_alert`
4. `admin.py` — 修复成功时调用 `record_admin_alert(resolved=True)` 清零计数
5. `admin.py` — `/api/reconciliation` 返回 order_map 新增的 4 个告警字段

### Phase 4: UI 层
1. 订单表新增"🚨 告警"列
2. 告警徽标 → 点击弹窗显示告警历史
3. 排序条件追加 `alert_count DESC`

### Phase 5: 日报增强
1. cron-f 生成日报时查询 `alert_log` 聚合
2. 嵌入 summary_json 的 `alert_summary` 字段

## 5. 与告警方案的依赖关系

```
告警方案 v0.3（基础告警覆盖）
  │
  ├─ alert_p2 方法（notifier 扩展）   ← 本方案依赖
  ├─ credentials.yaml 已配 webhook   ← 已就绪
  │
  └─ Admin 整合（本方案）
       ├─ record_admin_alert() 统一入口
       ├─ alert_log 表 + order_map 字段
       └─ admin UI 告警可视化
```

两个方案应**同时实施、同时部署**。

## 6. 验收标准

- [ ] `alert_log` 表存在，索引正确（idx_alert_log_order, idx_alert_log_created）
- [ ] `order_map` 新增 4 个告警列（alert_count, last_alert_level, last_alert_time, last_alert_message）
- [ ] `FeishuNotifier` 新增 `alert_p2` 方法 + P2→P1 升级逻辑
- [ ] `record_admin_alert()` 辅助函数实现（脱敏 + 写 alert_log + 更新 order_map + 调 notifier + 支持 resolved）
- [ ] admin resubmit 失败 → 飞书 P1 + 写入 alert_log + order_map 计数+1
- [ ] admin resubmit 成功 → 清零该订单 alert_count
- [ ] admin resubmit-lanmong 失败 → 飞书 P1/P2 + 写入 alert_log
- [ ] admin pull-* 异常 → 飞书 P2
- [ ] admin `GET /api/alerts` 端点可用，复用飞书 OAuth 鉴权
- [ ] admin 订单表显示告警状态列（alert_count > 0 显示红色/黄色徽标）
- [ ] 点击告警徽标 → 弹窗显示该订单告警历史
- [ ] 对账日报包含告警统计（summary_json 中 alert_summary）
- [ ] `alert_log` 清理策略实现（P0/P1 180 天，P2 90 天）
- [ ] 排序按 alert_count DESC 优先展示高告警订单
