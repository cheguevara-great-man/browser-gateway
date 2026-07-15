# Architecture

Browser Gateway contains two independent pieces:

1. A Manifest V3 Chrome extension that owns the proxy setting for one Chrome profile.
2. A hardened HTTPS forward proxy on a user-owned server.

The extension uses Chrome's standard `fixed_servers` configuration with an HTTPS proxy. It never opens a local TCP port. Local applications continue to use Browser AI Bridge on `127.0.0.1:18888`; Bridge requests executed by Chrome follow the active profile proxy.

The server runs a dedicated sing-box process, separate from any existing proxy service. TLS uses a publicly trusted Let's Encrypt short-lived IP certificate. HTTP proxy authentication is carried inside that TLS connection.

Server routing policy rejects private and self-referential destination addresses, permits destination ports 80 and 443 only, and rejects everything else. The service runs as an unprivileged system user with only `CAP_NET_BIND_SERVICE`.

When the extension is disabled it clears only its own Chrome proxy setting. Chrome then returns to the effective setting below it, such as the Windows system proxy managed by Clash rule mode.

\n