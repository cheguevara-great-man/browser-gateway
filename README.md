# Browser Gateway（浏览器网关）

Browser Gateway 是一个配合 Browser AI Bridge 使用的 Chrome HTTPS 代理扩展。它让 Chrome（以及 Bridge 在 Chrome 中执行的网络请求）通过你自己的境外服务器访问互联网，无需在 Windows 上安装 VPN 客户端。

## 适用范围

- 只代理当前 Chrome 配置文件，不是 Windows 全局 VPN。
- 不在本机开放通用的 SOCKS 或 HTTP 代理端口。
- 可以与 Clash 规则模式共存：启用本扩展时由本扩展接管 Chrome；关闭后清除本扩展设置。
- 如果 FanVPN 或其他扩展正在控制 Chrome 代理，本扩展会检测并提示冲突。
- 服务器端使用带身份验证和 TLS 加密的独立 HTTPS 代理。

## 工作原理

```text
VS Code Codex / Claude
        ↓
Browser AI Bridge（127.0.0.1:18888）
        ↓
Chrome + Browser Gateway 扩展
        ↓
自己的境外服务器
        ↓
境外互联网
```

更详细的说明见[架构文档](docs/ARCHITECTURE.md)和[安全说明](docs/SECURITY.md)。

## 目录结构

- `extension/`：可通过“加载已解压的扩展程序”安装的 Chrome Manifest V3 扩展。
- `server/`：可重复执行的 Debian 服务器安装脚本。
- `tools/`：部署、测试和诊断工具。
- `docs/`：架构与安全说明。

## 部署服务器

准备条件：

- 一台 Debian 服务器，外部可以访问其 22 和 443 端口。
- Windows 已有可登录服务器 `root` 用户的 SSH 密钥。
- Windows 已安装 Node.js 22 或更高版本。

在仓库根目录的 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy-server.ps1 `
  -Server '<服务器 IP>' `
  -IdentityFile "$HOME\.ssh\browser_gateway_ed25519"
```

安装脚本会：

1. 安装独立的 HTTPS 代理服务。
2. 生成随机用户名和密码。
3. 申请可信的 TLS 证书并配置自动续期。
4. 将连接信息保存到本机被 Git 忽略的文件中。

生成的密码不会显示在终端，也不会被提交到 GitHub。

## 安装 Chrome 扩展

1. 在 Chrome 中打开 `chrome://extensions`。
2. 开启右上角的**开发者模式**。
3. 点击**加载已解压的扩展程序**，选择本仓库中的 `extension` 文件夹。
4. 打开 **Browser Gateway**，依次点击**保存设置**和**开启代理**。
5. 点击**检测连接**，确认检测出口与服务器 IP 相同。

Chrome 同一时间只能由一个扩展控制代理。启用 Browser Gateway 前，请先关闭 FanVPN 或其他代理扩展。

Browser AI Bridge 仍需正常运行在 `127.0.0.1:18888`。Bridge 交给 Chrome 执行的请求会自动通过 Browser Gateway，无需修改 Windows 全局代理。

## 测试与检查

```powershell
npm test
npm run check
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\test-server.ps1
```

本项目已经完成真实服务器验证，包括扩展控制、代理身份验证、服务器路由限制、Chrome 出口检测和 Browser AI Bridge 联调。
