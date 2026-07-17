# 中央用量与 Credits 仪表盘

Browser Gateway 可以在同一台服务器上运行独立的 Token 用量收集器，供多台 Browser AI Bridge
汇总 Codex 用量。收集器使用服务器现有的可信 IP TLS 证书，默认监听 TCP `9443`，数据保存在
`/var/lib/browser-gateway/usage.sqlite3`。

部署前需要在云服务器防火墙或安全组中放行 TCP `9443`。Bridge 的上报请求仍封装在现有浏览器
代理连接中；该端口只用于服务器端经过 TLS 和 Bearer 密钥验证的统计 API。

## 安全设计

- 上报密钥和管理查询密钥相互独立；普通电脑不能读取汇总。
- 服务只接受固定字段的 Token 统计，不接受提示词、回复正文或任意扩展字段。
- `event_id` 是数据库主键，客户端断线重试不会重复计数。
- systemd 服务使用无登录权限的 `browser-gateway` 用户、只读系统目录和 128 MB 内存上限。
- Bridge 先写本地 outbox，再异步通过 Chrome 与已有 HTTPS 代理上报；中央服务故障不会阻塞聊天。
- 网页登录使用单独的随机密码、12 小时签名会话、CSRF 校验和 HTTPS 安全 Cookie。
- 2.7 客户端已经排队的旧事件仍可接收，可以逐台升级，无需六台电脑同时停机。

## 部署产生的凭据

运行 `tools/deploy-server.ps1` 后：

- `deployment.local.json` 增加 `usageCollectorUrl` 和 `usageReportToken`，可复制到需要上报的电脑；
- `usage-admin.local.json` 包含仪表盘地址、网页账号密码和汇总 API 权限，只应保存在管理电脑；
- 服务器 `/etc/browser-gateway/usage-credentials.json` 包含服务端密钥，不应下载或公开。

## 用网页查看

部署完成后，在管理电脑运行：

```powershell
$admin = Get-Content "$HOME\.browser-gateway\usage-admin.local.json" -Raw | ConvertFrom-Json
$admin.dashboardUrl
$admin.dashboardUsername
$admin.dashboardPassword
```

用浏览器打开输出的 `dashboardUrl`，再输入对应用户名和密码。页面提供：

- 7、30、90、180、366 天范围；
- 每台机器的请求数、原始 Token、估算 Credits、偏离平均值和追平建议；
- 按模型及推理档位汇总；
- 输入、缓存输入、输出 Token 的独立费率；
- 可选的周期总 Credits 预算和每台机器公平目标。

Codex Credits 按模型返回的实际输入、缓存输入和输出 Token 折算。内置费率来自当前官方
[Codex Rate Card](https://help.openai.com/en/articles/20001106-codex-rate-card)。未来模型不会套用一个猜测费率，
而会标记为“未定价”，管理员可以在网页中补充或修改费率。

仪表盘给出的是可审计的**估算消耗**。Pro 订阅包含额度、滚动限制、重置时间和账号剩余额度仍以
OpenAI Codex Usage 页面为准；中央服务没有读取账号 Cookie 或 Token。

## 用 PowerShell 查看

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\get-usage-summary.ps1 -Days 30
```

返回结果按机器统计请求数、输入 Token、缓存输入 Token、输出 Token、总 Token、估算 Credits、
相对平均值和最后上报时间。
Bridge 端的配置方法见 FanVPN Bridge 仓库的 `docs/TOKEN_USAGE.md`。
