# Browser Gateway（浏览器网关）

Browser Gateway 是一个供 Chrome 使用的私有 HTTPS 代理扩展。它让 Chrome，以及 Browser AI Bridge 交给 Chrome 执行的请求，通过你自己的境外服务器访问互联网；Windows 不需要安装 VPN 客户端，也不会开放本地通用代理端口。

当前版本为 **0.4.1**。服务器链路支持 HTTP/2 连接复用、账号认证、可信 TLS 证书、目标访问限制、健康检查和证书自动续期。

## 请求链路

```text
VS Code Codex / Claude
        ↓
Browser AI Bridge（127.0.0.1:18888）
        ↓
Chrome + Browser Gateway 扩展
        ↓ HTTPS + HTTP/2 + 账号认证
GOST 公网入口（服务器 TCP 443）
        ↓ 127.0.0.1 回环
sing-box 安全出口策略
        ↓
目标网站
```

这里的 sing-box 是 Browser Gateway 自己的独立实例，只负责阻止内网、服务器自身及非 Web 端口，不接管服务器上的其他代理服务。详见[架构说明](docs/ARCHITECTURE.md)。

## 适用范围

- 只接管安装扩展的 Chrome 配置文件，不是 Windows 全局代理。
- 可以与 Clash 规则模式共存；启用扩展时由扩展接管 Chrome，关闭后清除本扩展设置。
- Chrome 同一时间只能由一个扩展控制代理。启用前应关闭 FanVPN 或其他代理扩展。
- Browser AI Bridge 仍需正常运行在 `127.0.0.1:18888`。

## Chrome 路由模式

扩展启用后，可在弹窗的“代理模式”选择三种行为：

| 模式 | Chrome 请求如何走 | 是否经过美国服务器 |
|---|---|---|
| **规则模式**（新安装默认） | 本地解析目标地址；中国大陆 IPv4、本地和内网地址直连；其余网站使用 Gateway | 仅非中国大陆/未解析目标 |
| **全局模式** | 除本地/内网地址外，全部使用 Gateway | 是 |
| **直连模式** | Chrome 由扩展强制使用 `DIRECT`，不使用 Windows 系统代理 | 否 |

规则模式的国内判断在 Chrome 本地执行：扩展内置并二分查找中国大陆 IPv4 路由表，任何解析到该表内地址的国内网站都会直连，不依赖网站域名或手工白名单。路由表作为扩展代码随版本发布，Chrome 运行时不会下载第三方规则。IPv6-only 目标或本地 DNS 无法解析的目标会保守地走服务器；这避免把未知地址错误地当成国内直连。

从旧版升级后，已经启用的配置会先保留原来的**全局模式**行为，不会在升级时悄悄改变你的访问路径。要使用规则或直连模式，打开扩展、选择模式并保存后再点击“开启”。

## 一键部署服务器

准备条件：

- Debian 12（amd64）服务器，公网可访问 TCP 22、80、443；启用中央 Token 统计时还需在云防火墙/安全组放行 TCP 9443。
- Windows 已配置可以登录服务器 root 用户的 SSH 密钥。
- 本机安装了 Git、Node.js 22+ 和 PowerShell。

在仓库根目录的 **PowerShell** 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy-server.ps1 `
  -Server '<服务器公网 IP>' `
  -IdentityFile "$HOME\.ssh\browser_gateway_ed25519" `
  -LocalCredentialPath "$HOME\.browser-gateway\deployment.local.json"
```

端口默认是 443。脚本会安装经过 SHA-256 校验的 GOST 和 sing-box、申请 Let's Encrypt IP 证书、生成随机代理凭据、配置 systemd 服务及定时健康检查。重复执行同一命令可安全更新配置和程序。

凭据只保存在服务器 root 目录和本机指定的 JSON 文件中，不会显示在终端，也不会提交到 GitHub。脚本同时生成被 Git 忽略的 `extension/runtime-config.json`，供本机首次加载扩展时导入。

确认部署和测试成功后，可执行一次安全加固。该命令会先验证密钥登录，再禁用 SSH 密码登录并启用 Debian 自动安全更新：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\harden-server.ps1 `
  -Server '<服务器公网 IP>' `
  -IdentityFile "$HOME\.ssh\browser_gateway_ed25519"
```

加固后仍可用同一私钥登录 root，但不能再用 root 密码通过 SSH 登录。请保留云服务商控制台的救援入口。

如果之后确实需要恢复 root 密码 SSH，同时保留密钥登录，可运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\enable-password-ssh.ps1 `
  -Server '<服务器公网 IP>' `
  -IdentityFile "$HOME\.ssh\browser_gateway_ed25519"
```

该命令不会修改 root 密码，只会在确认服务器已有未锁定密码后恢复密码认证。密码登录的暴力破解风险高于纯密钥登录，应使用强密码并妥善保管。

## Gemini 使用 WARP 分流

当服务器原生出口不能使用 Gemini 时，可以让 Gemini API、Gemini 网页及其必要依赖统一经过 Cloudflare 官方 WARP（MASQUE）；普通 Google 搜索和其他网站继续使用服务器原生出口。配置方法、链路和验证命令见 [Gemini 的 WARP 分流](docs/GEMINI_WARP.md)。

## 安装 Chrome 扩展

1. 在 Chrome 打开 `chrome://extensions`。
2. 开启右上角的**开发者模式**。
3. 点击**加载已解压的扩展程序**，选择本仓库的 `extension` 文件夹。
4. 打开 **Browser Gateway**，确认服务器、端口和账号已导入；若没有导入，按下一节手工填写。
5. 依次点击**保存设置**、**开启代理**和**检测连接**。
6. 检测出口应等于服务器公网 IP，状态应显示“已连接”。

## 账号和密码填什么

这里填写的是 **Browser Gateway 代理账号**，不是 ChatGPT、Chrome、Windows 或 SSH 的账号。

部署电脑上的凭据文件位于你传给 `-LocalCredentialPath` 的位置。使用上面的示例时，在 PowerShell 中这样打开：

```powershell
notepad "$HOME\.browser-gateway\deployment.local.json"
```

字段对应关系：

| JSON 字段 | 扩展输入框 |
|---|---|
| `host` | 服务器 |
| `port` | 端口 |
| `username` | 用户名 |
| `password` | 密码 |
| `expectedIp` | 预期出口 IP |

首次配置时密码不能留空。以后保存过密码后，密码框留空表示保留现有密码。不要把凭据文件上传 GitHub、粘贴到公开聊天或通过公开链接传输。

## 验收和诊断

代码与扩展测试：

```powershell
npm.cmd test
npm.cmd run check
```

服务器安全策略和出口测试：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\test-server.ps1 `
  -CredentialPath "$HOME\.browser-gateway\deployment.local.json"
```

服务器服务、监听端口、证书和最近日志：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\server-status.ps1 `
  -Server '<服务器公网 IP>'
```

项目还提供可选的真实 Chromium 验收脚本，用来确认扩展接管、出口 IP、GitHub/ChatGPT 访问和代理 HTTP/2 会话。它只用于开发诊断，需要先安装 Playwright 及其 Chromium：

```powershell
npm.cmd install --no-save --package-lock=false playwright
npx.cmd playwright install chromium
node .\tools\test-chrome-proxy.mjs "$HOME\.browser-gateway\deployment.local.json"
```

输出中的 `proxyHttp2Events` 大于 0 表示 Chrome 已经对代理使用 HTTP/2。

## 目录

- `extension/`：Chrome Manifest V3 扩展。
- `server/install-h2.sh`：推荐的 HTTP/2 服务器安装器。
- `server/install.sh`：旧版单层 HTTP/1.1 安装器，仅用于回退。
- `tools/`：部署、状态、安全测试和真实浏览器验收工具。
- `docs/`：[架构说明](docs/ARCHITECTURE.md)、[安全说明](docs/SECURITY.md)与[中央 Credits 仪表盘](docs/TOKEN_USAGE.md)。
