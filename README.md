# Browser Gateway

A private Chrome HTTPS-proxy controller designed to work with Browser AI Bridge. It lets the Chrome profile—and therefore Bridge's Chrome executor—use a user-owned overseas server without installing a system VPN client.

## Scope

- Chrome-only network proxy; it is not a Windows VPN.
- Does not open a general-purpose local SOCKS or HTTP port.
- Coexists with Clash rule mode by taking control only while enabled and clearing its setting when disabled.
- Detects when FanVPN or another extension already controls Chrome's proxy.
- Uses a dedicated authenticated HTTPS proxy service on the server.

## Repository layout

- `extension/`: unpacked Chrome Manifest V3 extension.
- `server/`: idempotent Debian server installer.
- `tools/`: deployment, validation, and diagnostics.
- `docs/`: architecture and security decisions.

## Deploy the server

Prerequisites: a Debian server with ports 22 and 443 reachable, an SSH key accepted for `root`, and Node.js 22 or newer on Windows.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\deploy-server.ps1 `
  -Server '<server-ip>' `
  -IdentityFile "$HOME\.ssh\browser_gateway_ed25519"
```

The installer creates a dedicated, authenticated HTTPS proxy, obtains a trusted TLS certificate, and installs automatic certificate renewal. Generated credentials are copied only to ignored local files; they are never printed or committed.

## Load the Chrome extension

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked** and select the repository's `extension` directory.
3. Open **Browser Gateway**, choose **保存设置**, then **开启代理**.
4. Choose **检测连接** and confirm that the reported egress IP matches the server.

Only one Chrome proxy-controlling extension can be active at a time. Disable FanVPN or a similar extension before enabling Browser Gateway. Keep Browser AI Bridge running on `127.0.0.1:18888`; requests executed by its Chrome executor will then use this gateway.

## Validation

```powershell
npm test
npm run check
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\test-server.ps1
```

The extension, authenticated proxy, restricted server routing, Chrome egress, and Browser AI Bridge integration have been validated on a real deployment.
