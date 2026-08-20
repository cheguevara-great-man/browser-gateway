#!/usr/bin/env bash
set -euo pipefail

# Installs only the new server-side Codex executor.  It intentionally does not
# restart, rewrite, or reload the existing Browser Gateway proxy, usage web UI,
# GOST, sing-box, or Nginx configuration.

PUBLIC_IP="${1:?usage: install-codex-executor.sh <public-ip> [listen-port]}"
LISTEN_PORT="${2:-9444}"
APP_ROOT="/opt/browser-gateway"
STATE_ROOT="/var/lib/browser-gateway/codex-executor"
TLS_ROOT="/etc/browser-gateway/tls"
SOURCE_EXECUTOR="/root/browser-gateway-codex-executor.py"
SOURCE_CREDENTIALS="/root/browser-gateway-codex-credentials.py"
SOURCE_USAGE="/root/browser-gateway-usage-collector.py"

fail() { echo "codex-executor: $*" >&2; exit 1; }
[[ "$(id -u)" == "0" ]] || fail "installer must run as root"
[[ "$PUBLIC_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || fail "a public IPv4 address is required"
[[ "$LISTEN_PORT" =~ ^[0-9]+$ ]] && (( LISTEN_PORT >= 1024 && LISTEN_PORT <= 65535 )) || fail "listen port must be 1024-65535"
getent passwd browser-gateway >/dev/null || fail "Browser Gateway service account is not installed"
[[ -s "$SOURCE_EXECUTOR" ]] || fail "Codex executor source was not uploaded"
[[ -s "$SOURCE_CREDENTIALS" ]] || fail "Codex credential module was not uploaded"
[[ -s "$SOURCE_USAGE" || -s "$APP_ROOT/bin/usage_collector.py" ]] || fail "usage collector module is unavailable"
[[ -s "$TLS_ROOT/fullchain.pem" && -s "$TLS_ROOT/privkey.pem" ]] || fail "existing Browser Gateway TLS files are unavailable"
if ss -ltnH "sport = :${LISTEN_PORT}" | grep -q .; then
  fail "TCP port ${LISTEN_PORT} is already in use"
fi

install -d -o root -g root -m 0755 "$APP_ROOT/bin"
install -d -o browser-gateway -g browser-gateway -m 0700 "$STATE_ROOT"
install -o root -g root -m 0755 "$SOURCE_EXECUTOR" "$APP_ROOT/bin/codex_executor.py"
install -o root -g root -m 0644 "$SOURCE_CREDENTIALS" "$APP_ROOT/bin/codex_credentials.py"

cat >/etc/systemd/system/browser-gateway-codex-executor.service <<EOF
[Unit]
Description=Isolated server-side Codex executor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=browser-gateway
Group=browser-gateway
ExecStart=/usr/bin/python3 ${APP_ROOT}/bin/codex_executor.py --listen 0.0.0.0 --port ${LISTEN_PORT} --database /var/lib/browser-gateway/usage.sqlite3 --credentials ${STATE_ROOT}/auth.json --usage-module ${APP_ROOT}/bin/usage_collector.py --tls-cert ${TLS_ROOT}/fullchain.pem --tls-key ${TLS_ROOT}/privkey.pem
Restart=always
RestartSec=2s
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=${STATE_ROOT} /var/lib/browser-gateway
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
RestrictAddressFamilies=AF_INET AF_INET6
SystemCallArchitectures=native
CapabilityBoundingSet=
MemoryMax=192M
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
if [[ -s "${STATE_ROOT}/auth.json" ]]; then
  chown browser-gateway:browser-gateway "${STATE_ROOT}/auth.json"
  chmod 0600 "${STATE_ROOT}/auth.json"
  systemctl enable --now browser-gateway-codex-executor.service
  systemctl is-active --quiet browser-gateway-codex-executor.service || {
    journalctl -u browser-gateway-codex-executor.service -n 50 --no-pager >&2
    fail "executor did not start"
  }
else
  systemctl disable --now browser-gateway-codex-executor.service >/dev/null 2>&1 || true
  echo "Codex executor installed but inactive: upload the server ChatGPT account credentials first."
fi

echo "Codex executor installed independently on https://${PUBLIC_IP}:${LISTEN_PORT}/v1/codex"
