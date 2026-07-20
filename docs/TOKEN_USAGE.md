# 中央 Codex 用量与 Credits 服务

## 作用

Browser Gateway 在同一台服务器上运行一个独立的中央收集器，接收多台 Browser AI Bridge 的
匿名化用量事件与脱敏官方额度快照，保存到 SQLite，并通过 HTTPS 提供中文仪表盘和 JSON API。

默认端口和文件：

| 项目 | 位置 |
|---|---|
| 公网仪表盘与上报 API | TCP `9443`，Nginx TLS |
| 本机收集器 | `127.0.0.1:19443` |
| 数据库 | `/var/lib/browser-gateway/usage.sqlite3` |
| 服务端凭据 | `/etc/browser-gateway/usage-credentials.json` |
| systemd | `browser-gateway-usage.service` |

部署前需要在云防火墙或安全组放行 TCP `9443`。

## 计算模型

收集器读取事件中的真实输入、缓存输入和输出 Token，按官方
[Codex Rate Card](https://help.openai.com/en/articles/20001106) 的每百万 Token
费率计算标准 Credits。它不是在线计费 API，也不会抓取或解析帮助中心网页。

推理档位保持独立维度，但不修改标准单价；高推理带来的更多输出 Token会自然计入。速度档位为
Fast 时，GPT-5.6/5.5 应用 `2.5×`，GPT-5.4 应用 `2×`；倍率来源见
[Codex Speed 文档](https://learn.chatgpt.com/docs/agent-configuration/speed)。未知模型保持未定价状态。

## 官方额度快照

Bridge 3.0.0 每 5 分钟调用 Codex 客户端自己使用的 Usage 产品接口，并只上报：套餐类型、已用比例、
窗口秒数、重置时间、是否已达到限制。邮箱、用户 ID、账号 ID、Cookie 和登录 Token 不会进入事件
或中央数据库。

OpenAI 官方文档只承诺在 `Codex settings > Usage` 查看限制、剩余 Credit 或重置选项，没有把
个人账号 Usage 产品接口列为公开稳定 API。因此中央服务把该数据视为“最近观察快照”：接口变化或
同步失败时保留上一份并显示过期，不阻塞 Codex。

## 部署与升级

在 Windows 管理电脑运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy-server.ps1 `
  -Server '<服务器 IP>' `
  -IdentityFile "$HOME\.ssh\browser_gateway_ed25519" `
  -LocalCredentialPath "$HOME\.browser-gateway\deployment.local.json" `
  -UsageViewerCredentialPath "$HOME\.browser-gateway\usage-viewer.local.json" `
  -UsageAdminCredentialPath "$HOME\.browser-gateway\usage-admin.local.json"
```

脚本可重复运行：保留现有代理和上报 Token、迁移数据库、新增缺失字段、更新程序并重启服务。
Bridge 2.7/2.8 已排队事件继续兼容。

## 三类凭据

### `deployment.local.json`

分发给每台需要使用 Gateway 和上报的电脑，包含代理凭据、`usageCollectorUrl` 和
`usageReportToken`。上报 Token 只能写入事件，不能读取汇总。

### `usage-viewer.local.json`

可分发给六台需要查看数据的电脑，只包含网页 URL 和只读账号密码。只读账号可访问全部统计页面，
但修改预算或费率会返回 `403 administrator_required`。

### `usage-admin.local.json`

只保留在管理电脑。它包含管理员网页账号密码和 Bearer `adminToken`，能修改预算、费率并调用
`/v1/usage/summary`。不要把该文件作为普通机器配置分发。

部署脚本会为三个本地文件设置仅当前 Windows 用户可读写的 ACL；这些文件不会进入 Git。

## 网页页面

- `/dashboard`：设备日 Credits 折线图、统计期设备总额、官方已用/剩余比例与公平建议；
- `/dashboard/daily`：兼容旧链接，自动显示新总览；
- `/dashboard/machines`：机器对比；
- `/dashboard/machine?id=<机器ID>`：单机模型组合与每日历史；
- `/dashboard/models`：模型、推理、速度、倍率和 Token 构成；
- `/dashboard/settings`：管理员预算与费率设置。

页面查询支持 `?start=YYYY-MM-DD&end=YYYY-MM-DD`，最长 366 天。登录后的只读会话也能读取
`/v1/usage/summary`，因此以后可以把前端改成独立 JavaScript、React 或其他展示，而不用修改
数据库和聚合层。

管理员可以设置控制周期、周期总 Credits、均分设备数以及可选硬上限。硬上限只使用管理员明确
填写的预算，不使用根据官方百分比推算的额度，避免漏记用量或活动刷新造成误停。达到上限后客户端
最多在完成当前任务后阻止下一次模型请求；其他产品接口不受影响。

会话有效期 12 小时，Cookie 使用 `Secure`、`HttpOnly` 和 `SameSite=Strict`；所有修改表单使用
CSRF Token。登录失败有固定延迟，Nginx 对公网请求限速。

## API

### 上报

```http
POST /v1/usage/events
Authorization: Bearer <report token>
Content-Type: application/json
```

仅接受固定字段，单批最多 100 条。`event_id` 是数据库主键，重复上报不会重复计数。

### 脱敏额度快照与机器策略

```http
POST /v1/usage/quota
GET  /v1/usage/policy?machine_id=<机器ID>
Authorization: Bearer <report token>
```

普通机器只能提交固定结构的额度快照，并读取自身是否达到配置上限；不能读取全体明细或修改策略。

### 汇总

```http
GET /v1/usage/summary?days=30
Authorization: Bearer <admin token>
```

返回 `totals`、`machines`、`models`、`daily`、`quota`、预算、公平目标和未定价 Token。支持
`days=1..366` 或精确的 `start/end` 日期。管理员 Bearer Token 和已登录的只读/管理员网页会话均可读取。

PowerShell 工具：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\get-usage-summary.ps1 -Days 30
```

## 运维检查

```bash
systemctl status browser-gateway-usage.service
journalctl -u browser-gateway-usage.service -n 100 --no-pager
curl https://<服务器IP>:9443/health
sqlite3 /var/lib/browser-gateway/usage.sqlite3 'select count(*) from usage_events;'
```

备份时复制 `/var/lib/browser-gateway/usage.sqlite3` 及其可能存在的 `-wal`、`-shm` 文件，或先停止
统计服务再复制主数据库。数据库损坏或丢失不会影响 443 代理，但历史统计会丢失。

## 安全与隐私

- 普通机器没有读取汇总或修改配置的权限；
- 不接收提示词、回复、文件、Cookie、账号 Token 或 API Key；
- 固定事件字段和长度上限阻止把服务当成任意数据存储；
- systemd 使用无登录权限用户、只读系统目录和内存限制；
- Collector 只监听回环，公网 TLS 由 Nginx 终止；
- 上报先落本机 outbox，再异步发送，中央服务故障不会阻塞 Codex 回复。

## 解释统计结果

网页中的 Credits 是“按公开规则复算的估算消耗”，用途是比较机器和工作模式。它不代表账号官方
剩余 Credits，也无法推断准确重置时间。若网页估算与 OpenAI Usage 页面有差异，应优先检查：

1. 是否启用了 Fast；
2. 是否出现未知或研究预览模型；
3. 官方费率是否刚更新；
4. 是否存在直连模式用量未经过 Bridge；
5. 账号是否仍处于少数 Enterprise 旧费率卡。
