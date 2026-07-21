# Gemini 的 WARP 分流

当服务器原生出口无法使用 Gemini 时，可以让 **Gemini 相关域名单独经过 Cloudflare WARP**，其余流量仍使用服务器原生出口。

## 工作方式

```text
普通网站 / OpenAI / Claude
  Browser Gateway → 专属 sing-box → 服务器原生出口

Gemini 相关域名
  Browser Gateway → 专属 sing-box → 127.0.0.1:18089
  → 服务器已有 sing-box → Cloudflare WARP WireGuard 端点
```

`127.0.0.1:18089` 只监听服务器回环地址，不对公网提供代理。WARP 不会接管 SSH、系统更新或服务器的全部网络。

## 前提

服务器已有一套正常运行的系统 sing-box，并包含：

- 标签为 `wireguard-out` 的 Cloudflare WireGuard endpoint；
- 标签为 `gemini` 和 `google` 的规则集；
- Browser Gateway 已经部署完成。

脚本会复用这些现有配置，不会创建第二份 WARP 身份。Gemini 与 Google 规则集会复制为 Browser Gateway 的本地二进制规则集，因此 API、Gemini 网页、Google 登录和页面依赖的公共后端会保持同一 WARP 出口，也不依赖服务启动时临时下载规则。

## 启用

在项目根目录的 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure-gemini-warp.ps1 `
  -Server '<服务器公网 IP>' `
  -IdentityFile "$HOME\.ssh\browser_gateway_ed25519"
```

脚本会备份原配置、校验两套 sing-box 配置后再重启服务。如果校验或启动失败，会自动恢复备份。

启用状态保存在服务器的 `/etc/browser-gateway/gemini-warp.json`。以后重新运行常规服务器部署脚本时，Gemini 分流会自动保留。

## 验证

服务器上分别检查两个出口：

```bash
curl https://www.cloudflare.com/cdn-cgi/trace
curl --proxy socks5h://127.0.0.1:18089 https://www.cloudflare.com/cdn-cgi/trace
```

第一条应显示服务器原生 IP 和 `warp=off`；第二条应显示 Cloudflare 出口和 `warp=on`。

Cloudflare 官方说明 WARP 不是固定国家的匿名代理，因此最终还应以 Gemini 网站或 API 的实际返回结果为准。
