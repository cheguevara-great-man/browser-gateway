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

### 扩展中的账号和密码怎么填

扩展里填写的是 **Browser Gateway 代理服务器的账号和密码**，不是 ChatGPT、Chrome、SSH 或 Windows 的登录账号。

部署脚本会将完整连接信息保存在执行部署的电脑上：

```text
C:\Users\<Windows 用户名>\.browser-gateway\deployment.local.json
```

在这台电脑的 PowerShell 中运行下面的命令可以直接打开该文件：

```powershell
notepad "$HOME\.browser-gateway\deployment.local.json"
```

将文件字段对应填入扩展：

| JSON 字段 | 扩展输入框 |
|---|---|
| `host` | 服务器 |
| `port` | 端口 |
| `username` | 用户名 |
| `password` | 密码 |
| `expectedIp` | 预期出口 IP |

如果扩展已经保存过密码，密码框留空表示保留现有密码；首次配置或更换电脑时不能留空。

保存正确凭据后，扩展会自动响应与已配置服务器完全匹配的 Chrome 代理认证，不需要在
Chrome 的认证弹窗中重复输入。如果重启 Chrome 后仍出现弹窗，先在扩展中重新输入正确密码并
点击“保存设置”，再刷新扩展；只在 Chrome 弹窗中输入的密码仅由当前浏览器会话临时缓存。
扩展冷启动时如果检测到相同代理已经生效，不会重复写入 Chrome 代理设置，以免重置认证状态；
同时会提前载入已保存的凭据、覆盖 HTTP/HTTPS/WebSocket 认证挑战，并主动预热 Chrome 的
代理认证缓存，避免恢复标签页时出现原生账号密码弹窗。

在部署服务器的同一台电脑上，脚本还会生成被 Git 忽略的 `extension/runtime-config.json`，扩展首次加载时通常会自动导入这些信息。另一台电脑默认没有凭据文件，需要通过安全的离线方式取得上述字段后再填写。不要把凭据文件提交到 GitHub、粘贴到聊天记录或通过公开链接传输。

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
