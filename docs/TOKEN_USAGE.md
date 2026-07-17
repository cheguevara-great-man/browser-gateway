# 中央 Token 用量服务

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

## 部署产生的凭据

运行 `tools/deploy-server.ps1` 后：

- `deployment.local.json` 增加 `usageCollectorUrl` 和 `usageReportToken`，可复制到需要上报的电脑；
- `usage-admin.local.json` 包含汇总查询权限，只应保存在管理电脑；
- 服务器 `/etc/browser-gateway/usage-credentials.json` 包含服务端密钥，不应下载或公开。

## 查看汇总

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\get-usage-summary.ps1 -Days 30
```

返回结果按机器统计请求数、输入 Token、缓存输入 Token、输出 Token、总 Token 和最后上报时间。
Bridge 端的配置方法见 FanVPN Bridge 仓库的 `docs/TOKEN_USAGE.md`。
