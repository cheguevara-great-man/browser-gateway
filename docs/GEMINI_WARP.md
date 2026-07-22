# Gemini 的 WARP 分流

当服务器原生出口无法使用 Gemini 时，可以让 **Gemini 相关域名单独经过 Cloudflare WARP**，其余流量仍使用服务器原生出口。

## 当前链路

```text
Chrome → Browser Gateway → 服务器专属 sing-box
  ├─ 普通 Google 搜索及其他网站 → 服务器原生出口
  └─ Gemini 网页、API 与必要依赖 → 127.0.0.1:18090
       → Cloudflare 官方 WARP 客户端（MASQUE）→ Cloudflare 出口
```

`127.0.0.1:18090` 只监听服务器回环地址，不对公网开放。WARP 采用本地代理模式，不会接管 SSH、系统更新或服务器的全部网络，也不会修改已有 sing-box 工具中的其他路由。

使用官方 WARP 客户端而不是手写 WireGuard endpoint，是为了使用 MASQUE 的连接恢复、HTTP/3 与 HTTP/2 回退能力，降低空闲后偶发超时。普通 `google.com` 不经过 WARP，避免搜索流量与 Gemini 共用一条不必要的隧道。

## 启用或修复

在 Browser Gateway 项目根目录的 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure-gemini-warp.ps1 `
  -Server '服务器 IP' `
  -IdentityFile "$HOME\.ssh\browser_gateway_ed25519"
```

脚本会自动完成：

- 从 Cloudflare 官方软件源安装或升级 WARP 客户端；
- 注册 WARP、选择 MASQUE 并启用仅本机可见的 SOCKS5 代理；
- 仅将 Gemini 网页、API 和必要依赖分流到 WARP；
- 清理旧版本为 Browser Gateway 加入系统 sing-box 的回环入口，但保留用户自己的 endpoint、规则集和其他路由；
- 保存备份、检查配置并重启相关服务。

启用状态保存在 `/etc/browser-gateway/gemini-warp.json`，以后重新执行常规服务器部署时仍会保留。

## 服务器验证

```bash
warp-cli --accept-tos status
ss -ltnp | grep 18090
curl --proxy socks5h://127.0.0.1:18090 https://www.cloudflare.com/cdn-cgi/trace
curl --proxy socks5h://127.0.0.1:18090 -I https://gemini.google.com/app
curl -I https://www.google.com/
```

第一条应显示 `Connected`；监听地址应为 `127.0.0.1:18090`；Cloudflare trace 中应出现 `warp=on`。最后一条用于确认普通 Google 搜索仍走服务器原生出口。

Cloudflare 官方说明 WARP 不是固定国家的匿名代理，因此最终仍应以 Gemini 网站或 API 的实际返回结果为准。
