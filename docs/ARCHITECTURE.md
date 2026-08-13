# 架构说明

Browser Gateway 由 Chrome 扩展和服务器端两层网关组成。

## Chrome 扩展

扩展使用 Chrome 标准代理 API，并通过 `webRequest.onAuthRequired` 仅向已保存的服务器主机和端口提供代理凭据。扩展不会在 Windows 上监听 TCP 端口，也不会修改 Windows 全局代理。

扩展有三种 Chrome 配置：

- **全局模式**：`fixed_servers` HTTPS 代理；除回环/本地地址外都交给 Gateway。
- **规则模式**：本地 PAC；常用国内域名、本地和内网地址返回 `DIRECT`，其他目标返回 HTTPS Gateway。PAC 在本机执行，不向 Gateway 上传域名列表或浏览记录。
- **直连模式**：Chrome `direct`；扩展明确绕过 Gateway 和 Windows 系统代理。此模式不触发 Gateway 认证预热。

规则模式只使用随扩展发布的稳定国内域名规则，而非联网下载的动态规则集。这避免了浏览器启动时额外请求第三方规则服务，也使路由行为可审计；代价是少数未涵盖的国内 `.com` 域名会按“其他目标”走 Gateway。

扩展启动时会恢复已保存的代理状态并预热认证缓存。关闭代理时只清除由本扩展设置的 Chrome 代理。

## 服务器公网入口：GOST

GOST 监听 TCP 443，负责：

- TLS 1.3/1.2 加密和 Let's Encrypt 可信 IP 证书；
- HTTP/2 代理协议与多路复用；
- 代理用户名和密码认证；
- 把认证通过的请求转交给回环地址上的安全出口。

Chrome 首次建立 TLS 和 HTTP/2 会话需要一次网络往返；后续请求可以复用连接和并发流，减少重复握手。公网入口以无特权 `browser-gateway` 用户运行，只保留绑定 443 所需的能力。

## 服务器安全出口：sing-box

独立 sing-box 实例只监听 `127.0.0.1:18088`，不对公网开放，也不提供 VPN/隧道功能。它负责：

- 解析目标域名后执行地址规则；
- 拒绝回环、链路本地、私网及服务器自身地址；
- 只允许目标 TCP 80 和 443；
- 其余请求全部拒绝。

GOST 到 sing-box 只经过服务器本机回环。这个额外转发通常只有亚毫秒级开销，相比公网延迟可忽略；它换来了独立、容易验证的 SSRF 和端口安全边界。

本项目部署的 sing-box 是专属进程和配置，不会修改或复用服务器原有的 sing-box、Argo、Nginx 等服务。

## 运维组件

- `browser-gateway.service`：GOST HTTP/2 入口。
- `browser-gateway-egress.service`：sing-box 回环安全出口。
- `browser-gateway-health.timer`：每五分钟做一次带认证的端到端出口检查，连续失败三次才自动重启。
- `browser-gateway-cert-renew.timer`：每天两次检查短期 IP 证书；实际续期后原子替换证书并重启入口服务。

证书实际续期时会出现一次很短的入口重启，通常约一至两秒；正在进行的单个连接可能重试，但不会改变凭据和扩展配置。

## 性能边界

主要延迟来自客户端到境外服务器及服务器到目标网站的公网网络。服务器本机策略层不是主要瓶颈。真实 Chromium 验收应同时观察：

- 首次请求时间（包含 TLS、认证和 HTTP/2 建连）；
- 同一 Chrome 会话内的热请求时间；
- `proxyHttp2Events` 是否大于 0；
- 出口 IP 是否与服务器一致。
