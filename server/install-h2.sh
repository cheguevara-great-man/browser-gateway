#!/usr/bin/env bash
set -euo pipefail

PUBLIC_IP="${1:?usage: install-h2.sh <public-ip> [listen-port]}"
LISTEN_PORT="${2:-8443}"
GOST_VERSION="3.2.6"
GOST_SHA256="b39037b0380ea001fb3c0c28441c2e10bfc694f90682739a65b53e55dce5238b"
SING_BOX_VERSION="1.13.14"
SING_BOX_SHA256="f48703461a15476951ac4967cdad339d986f4b8096b4eb3ff0829a500502d697"
CERTBOT_VERSION="5.4.0"
APP_ROOT="/opt/browser-gateway"
CONFIG_ROOT="/etc/browser-gateway"
TLS_ROOT="$CONFIG_ROOT/tls"
CREDENTIALS_FILE="/root/browser-gateway-credentials.json"
WEBROOT="/var/www/html"
POLICY_PORT="18088"
GEMINI_WARP_SETTINGS="$CONFIG_ROOT/gemini-warp.json"
USAGE_PORT="9443"
USAGE_BACKEND_PORT="19443"
USAGE_SOURCE="/root/browser-gateway-usage-collector.py"
USAGE_CREDENTIALS="$CONFIG_ROOT/usage-credentials.json"
USAGE_ADMIN_FILE="/root/browser-gateway-usage-admin.json"
USAGE_VIEWER_FILE="/root/browser-gateway-usage-viewer.json"

fail() { echo "browser-gateway: $*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || fail "installer must run as root"
[[ "$PUBLIC_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || fail "a public IPv4 address is required"
IFS=. read -r ip1 ip2 ip3 ip4 <<< "$PUBLIC_IP"
for octet in "$ip1" "$ip2" "$ip3" "$ip4"; do
  ((10#$octet <= 255)) || fail "public IPv4 address contains an invalid octet"
done
[[ "$LISTEN_PORT" =~ ^[0-9]+$ ]] || fail "listen port must be numeric"
(( LISTEN_PORT >= 1 && LISTEN_PORT <= 65535 )) || fail "listen port is out of range"

if ss -ltnH "sport = :${LISTEN_PORT}" | grep -q .; then
  configured_port="$(jq -r '.services[0].addr // ""' "$CONFIG_ROOT/gost.json" 2>/dev/null | sed -n 's/^://p' || true)"
  if ! systemctl is-active --quiet browser-gateway.service || [[ "$configured_port" != "$LISTEN_PORT" ]]; then
    fail "TCP port ${LISTEN_PORT} is already used by another service"
  fi
fi
if ss -ltnH "sport = :${POLICY_PORT}" | grep -q . && ! systemctl is-active --quiet browser-gateway-egress.service; then
  fail "local policy port ${POLICY_PORT} is already used by another service"
fi
if ss -ltnH "sport = :${USAGE_PORT}" | grep -q . && ! systemctl is-active --quiet browser-gateway-usage.service; then
  fail "token usage port ${USAGE_PORT} is already used by another service"
fi
if ss -ltnH "sport = :${USAGE_BACKEND_PORT}" | grep -q . && ! systemctl is-active --quiet browser-gateway-usage.service; then
  fail "local token usage backend port ${USAGE_BACKEND_PORT} is already used by another service"
fi
[[ -s "$USAGE_SOURCE" ]] || fail "usage collector was not uploaded"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl jq nginx openssl python3-venv sqlite3 tar

if ! getent passwd browser-gateway >/dev/null; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin browser-gateway
fi
install -d -o root -g browser-gateway -m 0750 "$APP_ROOT/bin" "$CONFIG_ROOT" "$TLS_ROOT"
install -d -o browser-gateway -g browser-gateway -m 0750 /var/lib/browser-gateway
install -d -o root -g root -m 0700 /root/browser-gateway-backups

backup="/root/browser-gateway-backups/$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
backup_items=()
for item in "$CONFIG_ROOT" /etc/systemd/system/browser-gateway.service \
  /etc/systemd/system/browser-gateway-egress.service \
  /etc/systemd/system/browser-gateway-usage.service; do
  [[ -e "$item" ]] && backup_items+=("${item#/}")
done
if ((${#backup_items[@]})); then
  tar -C / -czf "$backup" "${backup_items[@]}"
  chmod 0600 "$backup"
fi

work_root="$(mktemp -d)"
trap 'rm -rf "$work_root"' EXIT

download_verified() {
  local url="$1" expected="$2" output="$3"
  curl --fail --location --silent --show-error --retry 3 --retry-all-errors "$url" --output "$output"
  local actual
  actual="$(sha256sum "$output" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || fail "checksum mismatch for $url"
}

gost_archive="$work_root/gost.tar.gz"
download_verified \
  "https://github.com/go-gost/gost/releases/download/v${GOST_VERSION}/gost_${GOST_VERSION}_linux_amd64.tar.gz" \
  "$GOST_SHA256" "$gost_archive"
mkdir "$work_root/gost"
tar -xzf "$gost_archive" -C "$work_root/gost"
gost_binary="$(find "$work_root/gost" -type f -name gost -print -quit)"
[[ -n "$gost_binary" ]] || fail "GOST binary missing from release archive"
install -o root -g root -m 0755 "$gost_binary" "$APP_ROOT/bin/gost.next"
mv -f "$APP_ROOT/bin/gost.next" "$APP_ROOT/bin/gost"

sing_archive="$work_root/sing-box.tar.gz"
download_verified \
  "https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-amd64.tar.gz" \
  "$SING_BOX_SHA256" "$sing_archive"
mkdir "$work_root/sing-box"
tar -xzf "$sing_archive" -C "$work_root/sing-box"
sing_binary="$(find "$work_root/sing-box" -type f -name sing-box -print -quit)"
[[ -n "$sing_binary" ]] || fail "sing-box binary missing from release archive"
install -o root -g root -m 0755 "$sing_binary" "$APP_ROOT/bin/sing-box.next"
mv -f "$APP_ROOT/bin/sing-box.next" "$APP_ROOT/bin/sing-box"

if [[ ! -x "$APP_ROOT/certbot/bin/certbot" ]]; then
  python3 -m venv "$APP_ROOT/certbot"
  "$APP_ROOT/certbot/bin/python" -m pip install --disable-pip-version-check "certbot==${CERTBOT_VERSION}"
fi

install -d -m 0755 "$WEBROOT/.well-known/acme-challenge"
challenge="browser-gateway-$(openssl rand -hex 8)"
printf '%s' "$challenge" > "$WEBROOT/.well-known/acme-challenge/$challenge"
served="$(curl --fail --silent --max-time 10 "http://${PUBLIC_IP}/.well-known/acme-challenge/${challenge}" || true)"
rm -f "$WEBROOT/.well-known/acme-challenge/$challenge"
[[ "$served" == "$challenge" ]] || fail "Nginx is not serving the ACME webroot on port 80"

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
    --argjson port "$LISTEN_PORT" \
    --arg username "$gateway_user" \
    --arg password "$gateway_password" \
    '{host:$host,port:$port,username:$username,password:$password,expectedIp:$host,transport:"https-h2"}' \
    > "$CREDENTIALS_FILE"
  chmod 0600 "$CREDENTIALS_FILE"
else
  tmp_credentials="$work_root/credentials.json"
  jq --argjson port "$LISTEN_PORT" '.port=$port | .transport="https-h2"' "$CREDENTIALS_FILE" > "$tmp_credentials"
  install -o root -g root -m 0600 "$tmp_credentials" "$CREDENTIALS_FILE"
fi
gateway_user="$(jq -er '.username' "$CREDENTIALS_FILE")"
gateway_password="$(jq -er '.password' "$CREDENTIALS_FILE")"

if [[ ! -s "$USAGE_CREDENTIALS" ]]; then
  report_token="$(openssl rand -hex 32)"
  admin_token="$(openssl rand -hex 32)"
  dashboard_admin_password="$(openssl rand -base64 24 | tr -d '\n' | tr '/+' '_-')"
  dashboard_viewer_password="$(openssl rand -base64 24 | tr -d '\n' | tr '/+' '_-')"
  session_secret="$(openssl rand -hex 32)"
  jq -n --arg report_token "$report_token" --arg admin_token "$admin_token" \
    --arg dashboard_admin_password "$dashboard_admin_password" \
    --arg dashboard_viewer_password "$dashboard_viewer_password" --arg session_secret "$session_secret" \
    '{report_token:$report_token,admin_token:$admin_token,dashboard_admin_username:"admin",dashboard_admin_password:$dashboard_admin_password,dashboard_viewer_username:"viewer",dashboard_viewer_password:$dashboard_viewer_password,session_secret:$session_secret}' > "$USAGE_CREDENTIALS.next"
  install -o root -g browser-gateway -m 0640 "$USAGE_CREDENTIALS.next" "$USAGE_CREDENTIALS"
  rm -f "$USAGE_CREDENTIALS.next"
fi
if ! jq -e '.dashboard_admin_password and .dashboard_viewer_password and .session_secret' "$USAGE_CREDENTIALS" >/dev/null; then
  dashboard_admin_password="$(openssl rand -base64 24 | tr -d '\n' | tr '/+' '_-')"
  dashboard_viewer_password="$(openssl rand -base64 24 | tr -d '\n' | tr '/+' '_-')"
  session_secret="$(openssl rand -hex 32)"
  jq --arg dashboard_admin_password "$dashboard_admin_password" \
    --arg dashboard_viewer_password "$dashboard_viewer_password" --arg session_secret "$session_secret" \
    '.dashboard_admin_username=(.dashboard_admin_username // .dashboard_username // "admin") |
     .dashboard_admin_password=(.dashboard_admin_password // .dashboard_password // $dashboard_admin_password) |
     .dashboard_viewer_username=(.dashboard_viewer_username // "viewer") |
     .dashboard_viewer_password=(.dashboard_viewer_password // $dashboard_viewer_password) |
     .session_secret=(.session_secret // $session_secret)' \
    "$USAGE_CREDENTIALS" > "$USAGE_CREDENTIALS.next"
  install -o root -g browser-gateway -m 0640 "$USAGE_CREDENTIALS.next" "$USAGE_CREDENTIALS"
  rm -f "$USAGE_CREDENTIALS.next"
fi
report_token="$(jq -er '.report_token' "$USAGE_CREDENTIALS")"
admin_token="$(jq -er '.admin_token' "$USAGE_CREDENTIALS")"
dashboard_admin_username="$(jq -er '.dashboard_admin_username' "$USAGE_CREDENTIALS")"
dashboard_admin_password="$(jq -er '.dashboard_admin_password' "$USAGE_CREDENTIALS")"
dashboard_viewer_username="$(jq -er '.dashboard_viewer_username' "$USAGE_CREDENTIALS")"
dashboard_viewer_password="$(jq -er '.dashboard_viewer_password' "$USAGE_CREDENTIALS")"
tmp_credentials="$work_root/client-credentials.json"
jq --arg usage_url "https://${PUBLIC_IP}:${USAGE_PORT}/v1/usage/events" \
   --arg report_token "$report_token" \
   '.usageCollectorUrl=$usage_url | .usageReportToken=$report_token' \
   "$CREDENTIALS_FILE" > "$tmp_credentials"
install -o root -g root -m 0600 "$tmp_credentials" "$CREDENTIALS_FILE"
jq -n --arg summary_url "https://${PUBLIC_IP}:${USAGE_PORT}/v1/usage/summary" \
  --arg dashboard_url "https://${PUBLIC_IP}:${USAGE_PORT}/dashboard" \
  --arg admin_token "$admin_token" --arg dashboard_username "$dashboard_admin_username" \
  --arg dashboard_password "$dashboard_admin_password" \
  '{summaryUrl:$summary_url,adminToken:$admin_token,dashboardUrl:$dashboard_url,dashboardUsername:$dashboard_username,dashboardPassword:$dashboard_password,role:"admin"}' > "$USAGE_ADMIN_FILE"
chmod 0600 "$USAGE_ADMIN_FILE"
jq -n --arg dashboard_url "https://${PUBLIC_IP}:${USAGE_PORT}/dashboard" \
  --arg dashboard_username "$dashboard_viewer_username" --arg dashboard_password "$dashboard_viewer_password" \
  '{dashboardUrl:$dashboard_url,dashboardUsername:$dashboard_username,dashboardPassword:$dashboard_password,role:"viewer"}' > "$USAGE_VIEWER_FILE"
chmod 0600 "$USAGE_VIEWER_FILE"
install -o root -g root -m 0755 "$USAGE_SOURCE" "$APP_ROOT/bin/usage_collector.py"

install -d -m 0755 /usr/local/libexec
cat > /usr/local/libexec/browser-gateway-refresh-cert <<EOF
#!/usr/bin/env bash
set -euo pipefail
install -o root -g browser-gateway -m 0640 /etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem ${TLS_ROOT}/fullchain.pem.next
install -o root -g browser-gateway -m 0640 /etc/letsencrypt/live/${PUBLIC_IP}/privkey.pem ${TLS_ROOT}/privkey.pem.next
mv -f ${TLS_ROOT}/fullchain.pem.next ${TLS_ROOT}/fullchain.pem
mv -f ${TLS_ROOT}/privkey.pem.next ${TLS_ROOT}/privkey.pem
if systemctl is-active --quiet browser-gateway.service; then
  systemctl restart browser-gateway.service
fi
if systemctl is-active --quiet browser-gateway-usage.service; then
  systemctl restart browser-gateway-usage.service
fi
nginx -t >/dev/null
systemctl reload nginx
EOF
chmod 0755 /usr/local/libexec/browser-gateway-refresh-cert
/usr/local/libexec/browser-gateway-refresh-cert

jq -n --argjson port "$POLICY_PORT" --argjson usage_port "$USAGE_PORT" --arg ip "$PUBLIC_IP" '{
  log:{level:"warn",timestamp:true},
  dns:{servers:[{type:"local",tag:"local"}]},
  inbounds:[{type:"http",tag:"policy-in",listen:"127.0.0.1",listen_port:$port}],
  outbounds:[{type:"direct",tag:"direct"}],
  route:{
    default_domain_resolver:"local",
    rules:[
      {action:"resolve",server:"local"},
      {ip_cidr:[($ip + "/32")],port:[$usage_port],action:"route",outbound:"direct"},
      {ip_cidr:[($ip + "/32")],action:"reject"},
      {ip_is_private:true,action:"reject"},
      {port:[80,443],action:"route",outbound:"direct"},
      {action:"reject"}
    ],
    final:"direct"
  }
}' > "$CONFIG_ROOT/egress.json.next"

# An optional, server-local Gemini split-routing profile is installed by
# configure-gemini-warp.sh.  Keeping the small marker under CONFIG_ROOT makes
# the policy survive normal Browser Gateway upgrades without coupling the base
# installer to a particular system-wide sing-box setup.
if [[ -s "$GEMINI_WARP_SETTINGS" ]] && jq -e '.enabled == true' "$GEMINI_WARP_SETTINGS" >/dev/null; then
  while IFS= read -r rule_set_path; do
    [[ -s "$rule_set_path" ]] || fail "Gemini WARP rule-set is missing: $rule_set_path"
  done < <(jq -er '.rule_sets[].path' "$GEMINI_WARP_SETTINGS")
  jq --slurpfile warp "$GEMINI_WARP_SETTINGS" '
    ($warp[0]) as $w |
    .outbounds += [{type:"socks",tag:"gemini-warp",server:"127.0.0.1",server_port:$w.proxy_port,version:"5"}] |
    .route.rule_set = ($w.rule_sets | map({type:"local",tag:.tag,format:"binary",path:.path})) |
    .route.rules = (
      .route.rules[0:4] +
      [{rule_set:($w.rule_sets | map(.tag)),port:[80,443],action:"route",outbound:"gemini-warp"}] +
      .route.rules[4:]
    )
  ' "$CONFIG_ROOT/egress.json.next" > "$CONFIG_ROOT/egress.json.warp"
  mv -f "$CONFIG_ROOT/egress.json.warp" "$CONFIG_ROOT/egress.json.next"
fi
install -o root -g browser-gateway -m 0640 "$CONFIG_ROOT/egress.json.next" "$CONFIG_ROOT/egress.json"
rm -f "$CONFIG_ROOT/egress.json.next"

cat > /etc/nginx/conf.d/browser-gateway-usage.conf <<EOF
limit_req_zone \$binary_remote_addr zone=browser_gateway_usage:10m rate=10r/s;
server {
  listen ${USAGE_PORT} ssl;
  server_name _;
  ssl_certificate ${TLS_ROOT}/fullchain.pem;
  ssl_certificate_key ${TLS_ROOT}/privkey.pem;
  ssl_protocols TLSv1.2 TLSv1.3;
  client_max_body_size 256k;
  location / {
    limit_req zone=browser_gateway_usage burst=30 nodelay;
    proxy_pass http://127.0.0.1:${USAGE_BACKEND_PORT};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header Connection "";
    proxy_connect_timeout 3s;
    proxy_read_timeout 15s;
  }
}
EOF
nginx -t
systemctl reload nginx

jq -n \
  --arg addr ":${LISTEN_PORT}" \
  --arg username "$gateway_user" \
  --arg password "$gateway_password" \
  --arg egress "127.0.0.1:${POLICY_PORT}" '{
    services:[{
      name:"browser-gateway-h2",
      addr:$addr,
      handler:{type:"http2",auth:{username:$username,password:$password},chain:"browser-gateway-egress"},
      listener:{type:"http2",tls:{certFile:"/etc/browser-gateway/tls/fullchain.pem",keyFile:"/etc/browser-gateway/tls/privkey.pem"}}
    }],
    chains:[{
      name:"browser-gateway-egress",
      hops:[{name:"policy",nodes:[{name:"sing-box-policy",addr:$egress,connector:{type:"http"},dialer:{type:"tcp"}}]}]
    }],
    log:{output:"stderr",level:"warn",format:"json"}
  }' > "$CONFIG_ROOT/gost.json.next"
install -o root -g browser-gateway -m 0640 "$CONFIG_ROOT/gost.json.next" "$CONFIG_ROOT/gost.json"
rm -f "$CONFIG_ROOT/gost.json.next"

cat > /etc/systemd/system/browser-gateway-egress.service <<'EOF'
[Unit]
Description=Browser Gateway policy egress
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=browser-gateway
Group=browser-gateway
ExecStart=/opt/browser-gateway/bin/sing-box run -c /etc/browser-gateway/egress.json
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
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK
SystemCallArchitectures=native
CapabilityBoundingSet=
LimitNOFILE=65536
MemoryMax=256M
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/browser-gateway.service <<'EOF'
[Unit]
Description=Private HTTP/2 HTTPS browser gateway
After=network-online.target browser-gateway-egress.service
Wants=network-online.target
Requires=browser-gateway-egress.service

[Service]
Type=simple
User=browser-gateway
Group=browser-gateway
ExecStart=/opt/browser-gateway/bin/gost -C /etc/browser-gateway/gost.json
ExecReload=/bin/kill -HUP $MAINPID
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
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
RestrictAddressFamilies=AF_INET AF_INET6
SystemCallArchitectures=native
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
LimitNOFILE=65536
MemoryMax=384M
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/browser-gateway-usage.service <<EOF
[Unit]
Description=Browser Gateway central token usage collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=browser-gateway
Group=browser-gateway
ExecStart=/usr/bin/python3 ${APP_ROOT}/bin/usage_collector.py --port ${USAGE_BACKEND_PORT} --database /var/lib/browser-gateway/usage.sqlite3 --credentials ${USAGE_CREDENTIALS}
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
ReadWritePaths=/var/lib/browser-gateway
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
RestrictAddressFamilies=AF_INET AF_INET6
SystemCallArchitectures=native
CapabilityBoundingSet=
MemoryMax=128M
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/libexec/browser-gateway-health <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
credentials=/root/browser-gateway-credentials.json
state=/run/browser-gateway-health-failures
host="$(jq -er '.host' "$credentials")"
port="$(jq -er '.port' "$credentials")"
user="$(jq -er '.username' "$credentials")"
password="$(jq -er '.password' "$credentials")"
proxy="https://${host}:${port}"
if egress="$(curl -4 --fail --silent --show-error --max-time 20 --proxy "$proxy" --proxy-user "${user}:${password}" https://api.ipify.org)" && [[ "$egress" == "$host" ]]; then
  rm -f "$state"
  exit 0
fi
failures=0
[[ -s "$state" ]] && read -r failures < "$state"
failures=$((failures + 1))
printf '%s\n' "$failures" > "$state"
if ((failures >= 3)); then
  systemctl restart browser-gateway-egress.service browser-gateway.service
  rm -f "$state"
fi
exit 1
EOF
chmod 0700 /usr/local/libexec/browser-gateway-health

cat > /etc/systemd/system/browser-gateway-health.service <<'EOF'
[Unit]
Description=Validate Browser Gateway end-to-end
After=browser-gateway.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/browser-gateway-health
EOF

cat > /etc/systemd/system/browser-gateway-health.timer <<'EOF'
[Unit]
Description=Periodic Browser Gateway end-to-end health check

[Timer]
OnBootSec=3m
OnUnitActiveSec=5m
RandomizedDelaySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/browser-gateway-cert-renew.service <<EOF
[Unit]
Description=Renew Browser Gateway IP certificate
After=network-online.target nginx.service

[Service]
Type=oneshot
ExecStart=${APP_ROOT}/certbot/bin/certbot renew --quiet --no-random-sleep-on-renew --cert-name ${PUBLIC_IP} --deploy-hook /usr/local/libexec/browser-gateway-refresh-cert
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

"$APP_ROOT/bin/sing-box" check -c "$CONFIG_ROOT/egress.json"
systemctl daemon-reload
systemctl enable browser-gateway-egress.service browser-gateway.service browser-gateway-usage.service \
  browser-gateway-health.timer browser-gateway-cert-renew.timer
systemctl restart browser-gateway-egress.service
systemctl restart browser-gateway.service
systemctl restart browser-gateway-usage.service
systemctl start browser-gateway-health.timer browser-gateway-cert-renew.timer
sleep 2
systemctl is-active --quiet browser-gateway-egress.service || fail "policy egress failed to start"
systemctl is-active --quiet browser-gateway.service || {
  journalctl -u browser-gateway.service -n 50 --no-pager >&2
  fail "HTTP/2 gateway failed to start"
}
systemctl is-active --quiet browser-gateway-usage.service || {
  journalctl -u browser-gateway-usage.service -n 50 --no-pager >&2
  fail "token usage collector failed to start"
}
ss -ltnH "sport = :${LISTEN_PORT}" | grep -q . || fail "gateway did not bind TCP ${LISTEN_PORT}"
ss -ltnH "sport = :${USAGE_PORT}" | grep -q . || fail "usage collector did not bind TCP ${USAGE_PORT}"
ss -ltnH "sport = :${USAGE_BACKEND_PORT}" | grep -q . || fail "usage collector backend did not bind TCP ${USAGE_BACKEND_PORT}"

echo "Browser Gateway HTTP/2 installed on TCP ${LISTEN_PORT}."
echo "Credentials remain in ${CREDENTIALS_FILE}."
echo "Usage administrator credentials remain in ${USAGE_ADMIN_FILE}."
echo "Usage read-only credentials remain in ${USAGE_VIEWER_FILE}."
