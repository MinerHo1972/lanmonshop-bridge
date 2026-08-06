# lanmonshop-bridge 飞书告警方案

> 状态: v0.3 · Codex audit reviewed · 2026-07-05

## 1. 告警机制概述

### 设计原则
- 飞书群机器人 webhook 为唯一推送通道（配置在 credentials.yaml）
- 告警 ≠ 日志：只有真正需要运营/开发关注的事件才推送
- P0 永不防抖：资损必须立即通知到人（@all），仅按 `order_no + condition` 去重
- P1 按 `category + order_no` 去重防噪，不按纯 category（避免多单被吞）
- P2 用 DB-backed counter + 现有检测模式（30min / 3次 / 1h cooldown），不用内存 dict
- 告警消息脱敏：不含手机号、地址、收件人等 PII

### 推送通道
```
credentials.yaml → FeishuNotifier(webhook_url) → httpx POST → 飞书群机器人 → 群消息
```

### 消息格式（标准飞书 webhook）
```json
{
  "msgtype": "text",
  "text": {
    "content": "【P1】JKY 创单失败\norder=LM20260705001, 重复订单"
  }
}
```

## 2. 告警分级标准

| 等级 | 含义 | 飞书动作 | 防抖策略 |
|------|------|----------|----------|
| P0 | 资损风险 / 数据不可逆损坏 | @all 全员 | 永不防抖，按 order_no+condition 去重 |
| P1 | 功能异常，单订单/单批次操作失败 | 普通消息 | category + order_no 去重，同 key 180s |
| P2 | 低优先级异常，不影响主流程 | 普通消息 | DB counter，30min 内 3 次升级 P1，1h cooldown |

### 判定逻辑
- **P0**: JKY 操作成功但蓝盟异常（资损）；创单成功但缺 tradeNo（重复创单风险）；JKY 已取消但蓝盟正常运行
- **P1**: 蓝盟 API 拉单异常（非无新订单）/ JKY 创单失败 / 回传 3 次重试耗尽 / 全量拉取全部窗口失败 / JKY 认证失败 / admin 订单卡住修复失败
- **P2**: 单次 SKU 缺缓存 / 部分拉取窗口失败 / 物流解析跳过 SLA 内 / 对账日报
- **P2→P1**: 同 category 30min 内累计 3 次失败 → 升级 P1 + 1h cooldown

## 3. 告警覆盖矩阵

| # | 位置 | 触发条件 | 等级 | 消息包含 | 状态 |
|---|------|----------|------|----------|------|
| 1 | cron-a §0 | SKU 缺映射（jky_product_cache 无对应）— 单个 | P2 | order_no, productNo | 已有，改为 P2 |
| 2 | cron-a §0 | SKU 缺映射 — 同 SKU 多单 / 批量超过 N 单 | P1 | 统计, productNo | 已有（P2→P1 升级） |
| 3 | cron-a §1 | 蓝盟拉单 page=1 API 返回异常（区别于无新订单） | P1 | 异常信息 | 待加，需 `pull_failed` 标志 |
| 4 | cron-a §5 | JKY trade_create 返回失败（code≠200） | P1 | order_no, 截断错误消息（脱敏） | 待加 |
| 5 | cron-a §5 | JKY trade_create 成功但响应缺 tradeNo | P0 | order_no, @all | 待加 |
| 6 | cron-b §3 | 物流回传 3 次重试耗尽 | P1 | order_no, jky_postid | 已有 |
| 7 | cron-b | JKY 全量拉取 — 全部窗口连续失败 | P1 | 异常信息 | 待加 |
| 8 | cron-b | JKY 全量拉取 — 部分窗口失败 | P2 | 窗口范围, 异常 | 待加 |
| 9 | cron-b | 物流解析/商品明细缺失 → 回传跳过（SLA 内） | P2 | order_no | 待加 |
| 10 | cron-c | JKY cancel 成功但蓝盟已取消 | P0 | order_no, @all | 已有 |
| 11 | cron-c | JKY 全量拉取 — 全部窗口失败 | P1 | 异常信息 | 待加 |
| 12 | cron-c | JKY 全量拉取 — 部分窗口失败 | P2 | 窗口范围 | 待加 |
| 13 | cron-d | 货品缓存刷新失败 | P1 | 异常信息 | **新增** |
| 14 | cron-e | 物流缓存刷新失败 | P1 | 异常信息 | **新增** |
| 15 | cron-f | JKY 全量拉取 — 全部窗口失败 | P1 | 异常信息 | 待加 |
| 16 | cron-f | 对账日报 | P2 | 统计数据 | 已有 |
| 17 | admin | 后台重新提交 — 纯输入/前置条件不满足 | 不告警 | — | 不变 |
| 18 | admin | 后台执行修复失败 + 订单仍卡住 | P1 | order_no, 错误原因 | 待加 |
| 19 | app.py | 各 cron 整体崩溃（未捕获异常） | P1 | 异常追溯 | 已有 |
| 20 | JKY | 认证失败（401/403/sign error） | P1 | 全链路, 错误类型 | **新增** |
| 21 | JKY | API 限流 — 单次 | P2 | 节流原因 | **新增** |
| 22 | JKY | API 限流 — 30min 窗口超过阈值 | P1 | 统计 | **新增** |

## 4. 实现方式

### 4.1 notifier 接口（app.py 已有，需适配）

```python
class FeishuNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.last_alert_time: dict[str, float] = {}
        self.fail_count: dict[str, int] = {}

    def alert_p0(self, title: str, detail: str):
        """P0: @all，永不防抖"""
        self._send(f"【P0】{title}", f"<at user_id='all'>所有人</at>\n{detail}")

    def alert_p1(self, title: str, detail: str, 
                    dedup_key: str = None, min_interval: int = 180):
        """P1: 按 category+order_no 去重"""
        if dedup_key:
            now = time.time()
            last = self.last_alert_time.get(dedup_key, 0)
            if now - last < min_interval:
                return
            self.last_alert_time[dedup_key] = now
        self._send(f"【P1】{title}", detail)

    def alert_p2(self, title: str, detail: str, category: str = ""):
        """P2: 通过 DB counter 跟踪，30min 内 3 次 → 升级 P1 + 1h cooldown"""
        category_key = f"p2:{category}"
        now = time.time()
        last = self.last_alert_time.get(category_key, 0)
        if now - last < 60:
            return  # P2 最少 60s 间隔
        count = self.fail_count.get(category_key, 0) + 1
        self.fail_count[category_key] = count
        if count >= 3:
            # 升级 P1
            detail += f"\n（同类别已累计 {count} 次，升级 P1）"
            self._send(f"【P1】{title}（升级）", detail)
            self.last_alert_time[category_key] = now
            self.fail_count[category_key] = 0
        else:
            self.last_alert_time[category_key] = now
            self._send(f"【P2】{title} ({count}/3)", detail)
```

### 4.2 消息格式

```python
def _send(self, title: str, content: str):
    payload = {
        "msgtype": "text",
        "text": {
            "content": f"{title}\n{content}"
        }
    }
    httpx.post(self.webhook_url, json=payload)
```

### 4.3 关键告警代码位置

#### cron-a Step 1 拉单失败（待加）

```python
pull_failed = False
for page in range(1, 999):
    try:
        orders = lanmong_client.get_orders(page=page, ...)
    except Exception as e:
        pull_failed = True
        if page == 1:
            notifier.alert_p1(
                "蓝盟拉单异常", f"page=1 失败: {str(e)[:200]}",
                dedup_key="cron-a:pull"
            )
        break
    # 正常拉单...
if not all_orders and not pull_failed:
    logger.info("无新订单，跳过")  # 不告警
```

#### cron-a Step 5 创单失败（待加）

```python
if jky_resp.get("code") != 200:
    notifier.alert_p1(
        "JKY 创单失败",
        f"order={order_no}, code={jky_resp.get('code')}, msg={str(jky_resp.get('message',''))[:200]}",
        dedup_key=f"cron-a:create:{order_no}"
    )
elif not jky_resp.get("data", {}).get("tradeNo"):
    # 成功响应但缺 tradeNo → P0 资损风险
    notifier.alert_p0(
        "JKY 创单成功但缺 tradeNo",
        f"order={order_no}, resp_id={jky_resp.get('requestId','')}"
    )
```

#### JKY 认证失败（待加）

```python
try:
    resp = jky_client.trade_list(...)
except JkyAuthError as e:
    notifier.alert_p1("JKY 认证失败", f"cron={name}, err={str(e)[:200]}")
    raise  # 认证失败应中断，不继续
except JkyRateLimitError as e:
    rate_counter.inc(name)
    if rate_counter.should_alert(threshold=10, window_sec=1800):
        notifier.alert_p1("JKY 限流超阈值", f"cron={name}, count={rate_counter.count}")
```

#### cron-d/e 货品/物流缓存刷新失败（待加）

```python
try:
    sync_product_cache(...)
except Exception as e:
    notifier.alert_p1("货品缓存刷新失败", f"err={str(e)[:200]}")
```

## 5. 配置

### 5.1 credentials.yaml

```yaml
feishu:
  webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx-xxx-xxx"
```

### 5.2 飞书群机器人配置

1. 飞书群 → 设置 → 群机器人 → 添加 Webhook 机器人
2. 复制 webhook URL → 写入 `credentials.yaml` 的 `feishu.webhook_url`
3. security 建议：设置 IP 白名单为 ECS 公网 IP (8.153.195.8)

## 6. 验收标准

- [ ] P0（JKY取消蓝盟正常 / 缺 tradeNo）→ 群 @all
- [ ] P1（拉单异常 / 创单失败 / 回传耗尽 / 全部窗口失败 / JKY认证失败 / cron-d/e 刷新失败 / admin 卡住）→ 群消息
- [ ] P2（SKU缺缓存单次 / 部分窗口失败 / 物流跳过 / 日报）→ 群消息
- [ ] 同类 P1/P2 错误按 category+order_no 去重，不刷屏
- [ ] P2 30min 内 3 次 → 升级 P1
- [ ] 告警消息不含手机号/地址/收件人
- [ ] cron 整体崩溃 → 飞书 P1
- [ ] 无新订单 → 不告警（区别于拉单异常）

## 7. 告警流程图

```
cron/admin 执行
   │
   ▼
业务逻辑成功 ──→ 正常推进（无告警）
   │
   ▼
业务逻辑失败
   │
   ├── 资损风险 / 关键数据丢失 → P0 @all（永不防抖）
   │
   ├── 全链路不可用 / 单订单失败 → P1
   │   └── category + order_no 去重（180s 同 key 静默）
   │
   ├── 局部失败 / 低优先级 → P2
   │   └── DB counter (30min / 3次 / 1h cooldown)
   │       └── ≥3次 → upgrade to P1
   │
   └── 纯输入/前置条件不满足 → 不告警（如无新订单）
```
