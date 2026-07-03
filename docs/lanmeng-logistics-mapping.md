# 蓝盟物流公司编码映射表

> 数据来源：蓝盟后台「物流公司管理」页面（2026-07-03 用户提供）
> 用途：cron-b 回传物流时 `logisticProvider` 传 code（非中文名）

| ID | 名称 | code |
|----|------|------|
| 1  | 圆通速递 | yuantong |
| 2  | 中通快递 | zhongtong |
| 3  | 韵达快递 | yunda |
| 4  | 申通快递 | shentong |
| 5  | 顺丰速运 | shunfeng |
| 6  | 邮政快递包裹 | youzhengguonei |
| 7  | 极兔速递 | jtexpress |
| 8  | 京东物流 | jd |
| 9  | EMS | ems |
| 10 | 邮政电商标快 | youzhengdsbk |

## 使用场景

### cron-b 回传物流（syncOrderExpress）

当前代码从 `lanmeng_logistic_name` 字段取中文名传值（如"圆通速递"），
蓝盟 syncOrderExpress API 期望 `logisticProvider` 字段传 code（如`yuantong`）。

需要用此映射表将中文名转为 code。

### 蓝盟 getLogisticsList API

如果后续需要动态拉取，蓝盟 `getLogisticsList` 接口返回的 `logisticCompanyName` 与上表 `名称` 对应，
同样需要映射为 code。
