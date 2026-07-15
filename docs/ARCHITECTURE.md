# 架构说明

Browser Gateway 由两个互相独立的部分组成：

1. 一个 Chrome Manifest V3 扩展，负责控制当前 Chrome 配置文件的代理设置。
2. 一个部署在用户自有服务器上的 HTTPS 正向代理服务。

## 请求链路

本扩展使用 Chrome 标准的 `fixed_servers` 设置，将服务器配置为 HTTPS 代理。扩展本身不会在 Windows 上打开本地 TCP 端口。

VS Code 中的 Codex 或 Claude 仍然连接本机的 Browser AI Bridge（`127.0.0.1:18888`）。Bridge 把需要联网的请求交给 Chrome 执行，Chrome 再按照当前配置文件的代理设置，将请求发送到 Browser Gateway 服务器。

## 服务器端

服务器使用独立的 sing-box 进程，不会修改或替换服务器上已有的其他代理服务。

外部连接使用 HTTPS，并使用 Let's Encrypt 签发的可信 IP 地址短期证书。代理用户名和密码只在加密的 TLS 连接中传输。

服务器路由策略会：

- 拒绝访问内网、回环、链路本地地址以及服务器自身地址。
- 只允许访问目标端口 80 和 443。
- 拒绝其他所有目标端口。

服务以无特权系统用户运行，只授予绑定低位端口所需的 `CAP_NET_BIND_SERVICE` 权限。

## 与其他代理扩展的关系

Chrome 同一时间只允许一个扩展控制代理。Browser Gateway 不会强行覆盖另一个扩展的设置，而是提示用户先关闭冲突扩展。

关闭 Browser Gateway 时，它只清除自己设置的 Chrome 代理。之后 Chrome 会恢复使用下层的有效设置，例如 Clash 规则模式管理的 Windows 系统代理。
