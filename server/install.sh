#!/usr/bin/env bash
set -euo pipefail

PUBLIC_IP="${1:?usage: install.sh <public-ip>}"
SING_BOX_VERSION="1.13.14"
SING_BOX_SHA256="f48703461a15476951ac4967cdad339d986f4b8096b4eb3ff0829a500502d697"
CERTBOT_VERSION="5.4.0"
APP_ROOT="/opt/browser-gateway"
CONFIG_ROOT="/etc/browser-gateway"
TLS_ROOT="$CONFIG_ROOT/tls"
CREDENTIALS_FILE="/root/browser-gateway-credentials.json"
WEBROOT="/var/www/html"

if [[ "$(id -u)" != "0" ]]; then
  echo "This installer must run as root." >&2
  exit 1
fi
if [[ ! "$PUBLIC_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "A public IPv4 address is required." >&2
  exit 1
fi
if ss -ltnH 'sport = :443' | grep -q . && ! systemctl is-active --quiet browser-gateway.service; then
  echo "TCP port 443 is already used by another service." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl jq openssl python3-venv tar

if ! getent passwd browser-gateway >/dev/null; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin browser-gateway
fi
install -d -o root -g browser-gateway -m 0750 "$APP_ROOT/bin" "$CONFIG_ROOT" "$TLS_ROOT"

archive="$(mktemp)"
extract_root="$(mktemp -d)"
trap 'rm -f "$archive"; rm -rf "$extract_root"' EXIT
curl --fail --location --silent --show-error \
  "https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz" \
  --output "$archive"
actual_sha256="$(sha256sum "$archive" | awk '{print $1}')"
if [[ "$actual_sha256" != "$SING_BOX_SHA256" ]]; then
  echo "sing-box archive checksum mismatch." >&2
  exit 1
fi
tar -xzf "$archive" -C "$extract_root" \
  "sing-box-${SING_BOX_VERSION}-linux-amd64/sing-box"
install -o root -g root -m 0755 \
  "$extract_root/sing-box-${SING_BOX_VERSION}-linux-amd64/sing-box" \
  "$APP_ROOT/bin/sing-box.next"
mv -f "$APP_ROOT/bin/sing-box.next" "$APP_ROOT/bin/sing-box"

if [[ ! -x "$APP_ROOT/certbot/bin/certbot" ]]; then
  python3 -m venv "$APP_ROOT/certbot"
  "$APP_ROOT/certbot/bin/python" -m pip install --disable-pip-version-check "certbot==${CERTBOT_VERSION}"
fi

install -d -m 0755 "$WEBROOT/.well-known/acme-challenge"
challenge="browser-gateway-$(openssl rand -hex 8)"
printf '%s' "$challenge" > "$WEBROOT/.well-known/acme-challenge/$challenge"
if [[ "$(curl --fail --silent --max-time 10 "http://${PUBLIC_IP}/.well-known/acme-challenge/${challenge}")" != "$challenge" ]]; then
  rm -f "$WEBROOT/.well-known/acme-challenge/$challenge"
  echo "Nginx is not serving the ACME webroot on port 80." >&2
  exit 1
fi
rm -f "$WEBROOT/.well-known/acme-challenge/$challenge"

if [[ ! -s "/etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem" ]]; then
  "$APP_ROOT/certbot/bin/certbot" certonly \
    --non-interactive \
    --agree-tos \
    --register-unsafely-without-email \
    --preferred-profile shortlived \
    --webroot \
    --webroot-path "$WEBROOT" \
    --cert-name "$PUBLIC_IP" \
    --ip-address "$PUBLIC_IP"
fi

if [[ ! -s "$CREDENTIALS_FILE" ]]; then
  gateway_user="gateway-$(openssl rand -hex 4)"
  gateway_password="$(openssl rand -base64 36 | tr -d '\n' | tr '/+' '_-')"
  jq -n \
    --arg host "$PUBLIC_IP" \
    --argjson port 443 \
    --arg username "$gateway_user" \
    --arg password "$gateway_password" \
    '{host:$host,port:$port,username:$username,password:$password,expectedIp:$host}' \
    > "$CREDENTIALS_FILE"
  chmod 0600 "$CREDENTIALS_FILE"
fi
gateway_user="$(jq -er '.username' "$CREDENTIALS_FILE")"
gateway_password="$(jq -er '.password' "$CREDENTIALS_FILE")"

install -d -m 0755 /usr/local/libexec
cat > /usr/local/libexec/browser-gateway-refresh-cert <<EOF
#!/usr/bin/env bash
set -euo pipefail
install -o root -g browser-gateway -m 0640 /etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem ${TLS_ROOT}/fullchain.pem
install -o root -g browser-gateway -m 0640 /etc/letsencrypt/live/${PUBLIC_IP}/privkey.pem ${TLS_ROOT}/privkey.pem
if systemctl is-active --quiet browser-gateway.service; then
  systemctl restart browser-gateway.service
fi
EOF
chmod 0755 /usr/local/libexec/browser-gateway-refresh-cert
/usr/local/libexec/browser-gateway-refresh-cert

jq -n \
  --arg ip "$PUBLIC_IP" \
  --arg username "$gateway_user" \
  --arg password "$gateway_password" \
  '{
    log: {level:"warn",timestamp:true},
    dns: {servers:[{type:"local",tag:"local"}]},
    inbounds:[{
      type:"http",
      tag:"browser-gateway-in",
      listen:"::",
      listen_port:443,
      users:[{username:$username,password:$password}],
      tls:{
        enabled:true,
        server_name:$ip,
        min_version:"1.2",
        alpn:["http/1.1"],
        certificate_path:"/etc/browser-gateway/tls/fullchain.pem",
        key_path:"/etc/browser-gateway/tls/privkey.pem"
      }
    }],
    outbounds:[{type:"direct",tag:"direct"}],
    route:{
      default_domain_resolver:"local",
      rules:[
        {action:"resolve",server:"local"},
        {ip_cidr:[($ip + "/32")],action:"reject"},
        {ip_is_private:true,action:"reject"},
        {port:[80,443],action:"route",outbound:"direct"},
        {action:"reject"}
      ],
      final:"direct"
    }
  }' > "$CONFIG_ROOT/config.json"
chown root:browser-gateway "$CONFIG_ROOT/config.json"
chmod 0640 "$CONFIG_ROOT/config.json"

cat > /etc/systemd/system/browser-gateway.service <<'EOF'
[Unit]
Description=Private HTTPS browser gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=browser-gateway
Group=browser-gateway
ExecStart=/opt/browser-gateway/bin/sing-box run -c /etc/browser-gateway/config.json
Restart=on-failure
RestartSec=3s
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK
SystemCallArchitectures=native
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
LimitNOFILE=65536
MemoryMax=384M
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/browser-gateway-cert-renew.service <<EOF
[Unit]
Description=Renew Browser Gateway IP certificate
After=network-online.target nginx.service

[Service]
Type=oneshot
ExecStart=${APP_ROOT}/certbot/bin/certbot renew --quiet --cert-name ${PUBLIC_IP} --deploy-hook /usr/local/libexec/browser-gateway-refresh-cert
EOF

cat > /etc/systemd/system/browser-gateway-cert-renew.timer <<'EOF'
[Unit]
Description=Twice-daily Browser Gateway certificate renewal

[Timer]
OnCalendar=*-*-* 00,12:17:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
EOF

"$APP_ROOT/bin/sing-box" check -c "$CONFIG_ROOT/config.json"
systemctl daemon-reload
systemctl enable browser-gateway.service browser-gateway-cert-renew.timer
systemctl restart browser-gateway.service
systemctl start browser-gateway-cert-renew.timer
sleep 1
if ! systemctl is-active --quiet browser-gateway.service; then
  journalctl -u browser-gateway.service -n 30 --no-pager >&2
  exit 1
fi
systemctl --no-pager --full status browser-gateway.service
echo "Browser Gateway server installed. Credentials remain in $CREDENTIALS_FILE."
