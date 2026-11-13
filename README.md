# 蓝盟-吉客云桥接服务（lanmonshop-bridge）

连接蓝盟商城中台与吉客云 ERP 的订单中转桥：中台产生销售订单 → 自动录入吉客云并过审 → 仓库发货后把物流单号回传中台，完成订单闭环。

完整需求与设计见 [`PRD.md`](PRD.md)、[`SPECIFICATION.md`](SPECIFICATION.md)、[`docs/DESIGN.md`](docs/DESIGN.md)。

## 运行形态

单一 Python(FastAPI) 服务 + APScheduler 定时任务 + SQLite。

| 项 | 值 |
|---|---|
| 生产主机 | 阿里云 ECS `8.153.195.8`（`root@`，私钥 `~/.ssh/id_rsa_alicloud`） |
| 服务单元 | `lanmonshop-bridge.service`（systemd，`WorkingDirectory=/opt/lanmonshop-bridge`） |
| 启动命令 | `/usr/bin/python3.11 -m uvicorn lanmeng_bridge.app:app --host 0.0.0.0 --port 18433` |
| 监听 | `0.0.0.0:18433`（健康检查 `GET /health`，根路径 307 跳 `/admin`） |
| 配置 | `/opt/lanmonshop-bridge/config/settings.yaml`（`SETTINGS_PATH`） |
| 凭据 | `/root/.hermes/data/credentials.yaml`（`CREDENTIALS_PATH`，600） |
| 数据库 | SQLite `/root/.hermes/data/lanmonshop-bridge.db`（WAL，约 100MB） |
| 依赖 | `requirements.txt`，Python 3.11，无虚拟环境（系统解释器） |

## 定时任务

| 任务 | 频率 | 作用 |
|---|---|---|
| cron-a | 5 分钟 | 中台 → 吉客云：拉取待发货订单，自动过审、创建吉客云销售单并审核 |
| cron-b | 60 分钟（兜底） | 吉客云 → 中台：拉已发货单，取物流单号，调 `syncOrderExpress` 回传；主路径是 webhook 实时（落在 hermes-web-api） |
| cron-c | 5 分钟 | 对账：中台已退、吉客云未退的孤儿单，触发吉客云取消；已发货的算 P0 资损告警 |
| cron-d | 每日 02:00 | 拉吉客云货品（`category IN ["饮料","周边"]`），刷新 `jky_product_cache`、增量同步 `sku_mapping` |
| cron-e | 每日 02:30 | 拉吉客云物流公司列表，刷新 `jky_logistic_cache` |
| webhook | 实时 | `/jky/webhook/oms.trade.confirm`，验签后调中台回传（落地在 hermes-web-api，与 cron-b 共享验签密钥与状态机） |

## 数据表速查

- `order_map` —— 订单映射与状态机主表：`state`（init/audited/jky_created/jky_shipped/synced/done/failed/jky_cancelled）、`platform_state`（蓝盟侧）、`jky_state`、`logistic_no`、`retry_count`、`last_error`
- `order_status_log` —— 状态流转审计，`from_state → to_state`、`source`（cron_a/cron_b/cron_c/webhook）
- `jky_product_cache` / `jky_logistic_cache` —— 货品与物流缓存（每日刷新）
- `reconciliation_report` —— cron-c 对账结果
- `api_call_log` —— 外部 API 调用日志，**只保留 7 天**，`request_body`/`response_body` 入库前截断到 4KB
- `express_sync` / `express_sync_items` —— **已废弃，代码从不写入**，回传结果看 `order_map.logistic_no` 与 `order_status_log`。查问题时不要拿这两张表的空结果当故障（见 commit `4a34204` 前后的讨论）
- `admin_users` / `sessions` / `alert_log` / `alert_counter` / `cron_cursor` —— 后台登录、告警与 cron 游标

## 部署与回滚

当前没有自动化部署脚本，按下面步骤手工发布（补自动化是接手后的待办）：

```bash
# 1. 本地取最新代码并跑测试
git clone https://github.com/MinerHo1972/lanmonshop-bridge.git && cd lanmonshop-bridge
pip install -r requirements.txt     # 或按现有环境安装
pytest tests/                       # 单测在 tests/，有 SQLite 依赖

# 2. 备份线上目录与数据库（发布前必做）
ssh root@8.153.195.8 'cp -a /opt/lanmonshop-bridge /opt/lanmonshop-bridge.bak-$(date +%Y%m%d-%H%M)'
ssh root@8.153.195.8 'sqlite3 /root/.hermes/data/lanmonshop-bridge.db ".backup /root/lanmonshop-bridge.db.bak"'

# 3. 传代码（不覆盖 config/ 与 .env 类文件）
rsync -av --exclude 'config/' --exclude '*.db*' ./ root@8.153.195.8:/opt/lanmonshop-bridge/

# 4. 重启并验证三样
ssh root@8.153.195.8 'systemctl restart lanmonshop-bridge'
ssh root@8.153.195.8 'systemctl is-active lanmonshop-bridge && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18433/health'
curl -s -o /dev/null -w "%{http_code}\n" http://8.153.195.8:18433/health

# 5. 回滚：目录换回 .bak 目录，再 restart，最后再验 /health
```

发布后要看 `journalctl -u lanmonshop-bridge -n 50` 有没有 cron 报错，并抽查 `order_status_log` 最新几条是否正常流转。

## 排障入口

- 服务：`systemctl status lanmonshop-bridge`、`journalctl -u lanmonshop-bridge -n 200`
- 健康：`curl http://127.0.0.1:18433/health`
- 卡单：查 `order_map` 里 `state` 停在 `failed`/`jky_created` 且 `last_error` 非空的记录
- 接口失败：`api_call_log` 按 `source`（`jky_direct` / `jky_gateway` / `lanmong`）看最近调用与返回码
- 后台：`http://8.153.195.8:18433/admin`

## 已知缺口（接手后待补）

1. **数据库无备份**：`/root/.hermes/data/lanmonshop-bridge.db` 目前没有任何定期备份。
2. **无监控告警**：服务挂了、cron 卡住，没有自动发现。
3. **部署靠手工**：没有 deploy 脚本与发布记录。
4. **外网直连 18433**：建议安全组收敛，只留必要入口。
