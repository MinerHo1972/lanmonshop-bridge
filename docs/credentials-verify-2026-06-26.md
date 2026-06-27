# 凭据持久化 + 端到端真验 (2026-06-26)

> **关联**: lanmonshop-bridge PRD v0.3 §7.1 #1 蓝盟凭据 + 失能事故根因修复
> **验证时间**: 2026-06-26 (Asia/Shanghai)
> **审计等级**: Schema-Auditor 一等公民 (credentials.yaml 链路 + 端到端真验)

---

## 1. 失能事故根因 + 修复

**根因** (2026-06-26 user 揭露):
- AI 推断"凭据不入 commit/记忆/inventory"硬约束 = 规则制定权错位（应是 user）
- 结果 = AI 失能（不能维护 launch-tracker 数据 = 违背 user 治理哲学"AI 是数据生产+门槛校验+文档化执行者"）

**修复** (2026-06-26 user 拍板 A):
- 凭证持久化入 `~/.hermes/data/credentials.yaml` (chmod 600, gitignore 屏蔽)
- 新硬约束入 USER profile: AI 不允许凭想象虚构硬规则

---

## 2. 凭据 schema 落地（v3）

```yaml
# ~/.hermes/data/credentials.yaml  (间接 verify，不 echo 完整 secret)
version: 3
updated_at: 2026-06-26
credentials:
  ecs:
    host: 8.153.195.8
    user: root
    auth_method: ssh_key
    ssh_key_path: ~/.ssh/id_rsa_alicloud
    # SSH_KEY_OK verified 2026-06-26 14:15 CST
  lanmenshop:
    appkey: sop***  (len=8)
    secret: **********  (32 chars)
    auth_method: appkey+sign
    endpoint_base: https://test-zt-api.lanmonshop.com  # 测试环境域名
    key_type: test                                     # ⚠️ 测试密钥，非正式生产凭据（user 2026-06-26 拍板）
    production_key_pending: true                       # 正式密钥待就位后替换
    migration_notes: 正式密钥就位时走 §6 SOP 4 步替换
```

**git ignore verify**: `git check-ignore -v data/credentials.yaml` → `.gitignore:19:data/` 命中 = git 不感知此文件

---

## 3. 端到端真验

**方法**: `curl -X POST https://test-zt-api.lanmonshop.com/open/v1/order/getDeliverOrders`

**结果**:
- HTTP 401 (0.763s, 59 bytes)
- 响应体: `{"code":-1,"msg":"未能从请求头中获得授权信息"}`

**解读**（Schema-Auditor 透明 disclose，不掩盖）:
- ✅ **链路通**: endpoint 真活、服务端能识别请求 = 网络 + DNS + 证书全 OK
- ⚠️ **凭据 sign 待 verify**: 因 sign 缺失（PRD §4.4 sign 字段未详），401 是预期
- ⏸️ **scope 1 待办**: 需补 sign 算法实现（user 2026-06-26 推进）

---

## 4. 安全实践教训（事故 + 改进）

### 4.1 事故
- user 2026-06-26 贴 secret 明文到飞书 DM = 已被 tencentDB 会话搜索层捕获（搜 "lanmenshop" / "appkey" / "secret" 可命中）
- **不可撤回**（对话历史已持久化）

### 4.2 改进（user 拍板）
- **敏感凭据走专用安全通道**（如 `ssh user@host "cat > /path"` 直传到 ECS，不经任何 IM）
- **凭据 schema 写入**用 env var + python（不在命令字符串/grep/bash history 暴露）
- **凭据 verify** 用间接 print（prefix/len，不 echo 完整 secret）
- **临时文件**用 `rm -f` 清理（避免 /tmp 残留）
- **应用层 verify** 用 HTTP code（不读完整 body 避免 secret/订单数据二次泄露）

### 4.3 写流程审计（本次执行）
- ✅ `python3 <<'PYEOF'` heredoc（secret 在 env var，不在命令字符串）
- ✅ `chmod 600` 落地
- ✅ `git check-ignore` 验证（不感知 credentials.yaml）
- ✅ 间接 verify（prefix/len/字段名）
- ✅ curl 端到端真验（HTTP 401 解读为链路通 + sign 待 verify）
- ✅ 响应体 500 字符截断 + `rm -f` 清理

---

## 5. scope 0 + scope 1 状态

### scope 0（ECS 资源基线 — 已闭环）
- ✅ 2026-06-26 14:15 CST 闭环
- 详见 `docs/baseline-2026-06-26.md`

### scope 1（外部依赖 verify — 部分闭环）
- ✅ 蓝盟 appKey + appSecret 入 credentials.yaml（链路通，**测试密钥**）
- ⏸️ sign 算法实现（PRD §4.4 待 confirm，需 user 推进）
- ⏸️ 吉客云 OTS 3 method 订阅 verify（user 推进）
- ⏸️ 吉客云物流公司编码清单查询（user 推进）
- ⏸️ `oms.trade.ordercancel` 适用范围 verify（user 推进）
- ⏸️ **正式密钥就位时走 §6 SOP**

---

## 6. 测试→正式密钥迁移 SOP（user 2026-06-26 拍板占位）

**触发条件**: 蓝盟中台正式密钥申请下来时

**4 步替换 SOP**:

### Step 1 — 正式密钥入 schema
- `~/.hermes/data/credentials.yaml` v4 → v5
- python 安全写入（env var + heredoc，secret 不进命令字符串）
- 字段更新:
  - `key_type: test` → `key_type: production`
  - `production_key_pending: true` → `production_key_pending: false`
  - `appkey` / `secret` 替换为正式值
  - `endpoint_base`: `test-zt-api.lanmonshop.com` → 正式 API 域名（user 告知）
  - `updated_at`: 替换日
- `chmod 600` 重设

### Step 2 — 端到端真验（重跑 §3）
- curl 蓝盟正式 API endpoint
- HTTP code verify（不读完整 body）
- 解读：链路通 / 凭据 sign 验真 / 业务响应正常

### Step 3 — audit doc 更新
- 本文件 §2 schema 更新（key_type 标 production + 移除 migration_notes）
- §3 加 v5 验证记录
- §5 scope 1 状态更新（凭据 sign 验真 = scope 1 部分闭环）

### Step 4 — memory / scene block 同步
- `~/.hermes/memories/USER.md` 同步："蓝盟凭据 = 正式密钥（v5 拍板）"
- 不入 tencentDB（凭据 security filter 主动拒绝，design by security）
- 业务服务 `lanmenshop-bridge` 启动时读 `credentials.yaml` v5（生产读正式密钥）

**安全实践**:
- 正式密钥仍走专用安全通道（不通过 IM 贴入）
- schema 写入用 python env var（不 echo 完整 secret）
- 旧测试密钥从 `credentials.yaml` 删除（不留残值）
