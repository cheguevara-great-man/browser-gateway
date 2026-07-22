#!/usr/bin/env bash
set -euo pipefail

WARP_PROXY_PORT="${WARP_PROXY_PORT:-18090}"
GATEWAY_CONFIG_ROOT="${GATEWAY_CONFIG_ROOT:-/etc/browser-gateway}"
GATEWAY_CONFIG="$GATEWAY_CONFIG_ROOT/egress.json"
GATEWAY_BINARY="${GATEWAY_BINARY:-/opt/browser-gateway/bin/sing-box}"
SETTINGS="$GATEWAY_CONFIG_ROOT/gemini-warp.json"
GEMINI_RULE_SET="$GATEWAY_CONFIG_ROOT/gemini.srs"
BACKUP_ROOT="/root/browser-gateway-backups"
EGRESS_DROPIN_DIR="/etc/systemd/system/browser-gateway-egress.service.d"
EGRESS_DROPIN="$EGRESS_DROPIN_DIR/gemini-warp.conf"

# Legacy files created by Browser Gateway 0.1.x.  They are removed after the
# official WARP local proxy is healthy; the user's other sing-box configuration
# and WireGuard endpoint are deliberately left untouched.
GLOBAL_CONFIG_DIR="${GLOBAL_CONFIG_DIR:-/etc/sing-box/conf}"
GLOBAL_ROUTE="$GLOBAL_CONFIG_DIR/route.json"
LEGACY_GLOBAL_INBOUND="$GLOBAL_CONFIG_DIR/browser-gateway-warp.json"
GLOBAL_BINARY="${GLOBAL_BINARY:-/etc/sing-box/sing-box}"

fail() { echo "gemini-warp: $*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || fail "run as root"
[[ -x "$GATEWAY_BINARY" ]] || fail "Browser Gateway sing-box binary not found at $GATEWAY_BINARY"
[[ -s "$GATEWAY_CONFIG" ]] || fail "Browser Gateway egress config not found at $GATEWAY_CONFIG"
[[ "$WARP_PROXY_PORT" =~ ^[0-9]+$ ]] || fail "WARP proxy port must be numeric"
(( WARP_PROXY_PORT >= 1024 && WARP_PROXY_PORT <= 65535 )) || fail "WARP proxy port is out of range"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl gpg jq lsb-release
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg |
  gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
  > /etc/apt/sources.list.d/cloudflare-client.list
apt-get update -qq
apt-get install -y -qq --no-install-recommends cloudflare-warp

install -d -o root -g root -m 0700 "$BACKUP_ROOT"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$BACKUP_ROOT/gemini-warp-$stamp"
install -d -o root -g root -m 0700 "$backup"
cp -a "$GATEWAY_CONFIG" "$backup/egress.json"
[[ -e "$SETTINGS" ]] && cp -a "$SETTINGS" "$backup/gemini-warp.json"
[[ -e "$GEMINI_RULE_SET" ]] && cp -a "$GEMINI_RULE_SET" "$backup/gemini.srs"
[[ -e "$EGRESS_DROPIN" ]] && cp -a "$EGRESS_DROPIN" "$backup/egress-dropin.conf"
[[ -e "$GLOBAL_ROUTE" ]] && cp -a "$GLOBAL_ROUTE" "$backup/global-route.json"
[[ -e "$LEGACY_GLOBAL_INBOUND" ]] && cp -a "$LEGACY_GLOBAL_INBOUND" "$backup/legacy-global-inbound.json"

rollback() {
  cp -a "$backup/egress.json" "$GATEWAY_CONFIG"
  if [[ -e "$backup/gemini-warp.json" ]]; then
    cp -a "$backup/gemini-warp.json" "$SETTINGS"
  else
    rm -f "$SETTINGS"
  fi
  if [[ -e "$backup/gemini.srs" ]]; then
    cp -a "$backup/gemini.srs" "$GEMINI_RULE_SET"
  else
    rm -f "$GEMINI_RULE_SET"
  fi
  if [[ -e "$backup/egress-dropin.conf" ]]; then
    install -d -o root -g root -m 0755 "$EGRESS_DROPIN_DIR"
    cp -a "$backup/egress-dropin.conf" "$EGRESS_DROPIN"
  else
    rm -f "$EGRESS_DROPIN"
  fi
  [[ -e "$backup/global-route.json" ]] && cp -a "$backup/global-route.json" "$GLOBAL_ROUTE"
  if [[ -e "$backup/legacy-global-inbound.json" ]]; then
    cp -a "$backup/legacy-global-inbound.json" "$LEGACY_GLOBAL_INBOUND"
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl restart sing-box.service browser-gateway-egress.service >/dev/null 2>&1 || true
}
trap 'rollback' ERR

# WARP proxy mode never changes the server's default route.  The official client
# uses MASQUE and exposes SOCKS5 only on 127.0.0.1.
systemctl enable --now warp-svc.service
if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
  warp-cli --accept-tos registration new
fi
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port "$WARP_PROXY_PORT"
warp-cli --accept-tos tunnel protocol set MASQUE
warp-cli --accept-tos connect

for _ in {1..40}; do
  if [[ "$(warp-cli --accept-tos --json status 2>/dev/null | jq -r '.status // empty')" == "Connected" ]] &&
     ss -ltnH "sport = :${WARP_PROXY_PORT}" | grep -q '127.0.0.1'; then
    break
  fi
  sleep 0.5
done
[[ "$(warp-cli --accept-tos --json status | jq -r '.status // empty')" == "Connected" ]] || fail "Cloudflare WARP did not connect"
ss -ltnH "sport = :${WARP_PROXY_PORT}" | grep -q '127.0.0.1' || fail "WARP proxy is not loopback-only"
curl --fail --silent --show-error --max-time 20 \
  --proxy "socks5h://127.0.0.1:${WARP_PROXY_PORT}" \
  https://www.cloudflare.com/cdn-cgi/trace | grep -q '^warp=on$' || fail "WARP proxy health check failed"

# Reuse an already downloaded Gemini binary ruleset when available.  On a fresh
# server the explicit domain list below is sufficient, so the feature does not
# depend on the user's system sing-box rules.
rule_sets='[]'
if [[ -s "$GEMINI_RULE_SET" ]]; then
  rule_sets="$(jq -n --arg path "$GEMINI_RULE_SET" '[{tag:"gemini-warp-gemini",path:$path}]')"
fi
jq -n --argjson port "$WARP_PROXY_PORT" --argjson rule_sets "$rule_sets" '{
  enabled:true,
  implementation:"cloudflare-warp-masque",
  proxy_port:$port,
  rule_sets:$rule_sets,
  web_domain_suffix:[
    "gemini.google.com",
    "gemini.google",
    "gemini.gstatic.com",
    "accounts.google.com",
    "aistudio.google.com",
    "ai.google.dev",
    "generativelanguage.googleapis.com",
    "geminiweb-pa.clients6.google.com",
    "waa-pa.clients6.google.com",
    "proactivebackend-pa.googleapis.com",
    "clients6.google.com",
    "content-push.googleapis.com",
    "ogads-pa.googleapis.com",
    "csp.withgoogle.com"
  ]
}' > "$SETTINGS.next"
install -o root -g browser-gateway -m 0640 "$SETTINGS.next" "$SETTINGS"
rm -f "$SETTINGS.next"

jq --slurpfile warp "$SETTINGS" '
  ($warp[0]) as $w |
  .outbounds = ([.outbounds[] | select(.tag != "gemini-warp")] +
    [{type:"socks",tag:"gemini-warp",server:"127.0.0.1",server_port:$w.proxy_port,version:"5"}]) |
  .route.rule_set = ([.route.rule_set[]? | select(.tag | startswith("gemini-warp-") | not)] +
    ($w.rule_sets | map({type:"local",tag:.tag,format:"binary",path:.path}))) |
  .route.rules = (
    [.route.rules[] | select(.outbound != "gemini-warp")] |
    .[0:4] +
    ((if ($w.rule_sets | length) > 0 then
       [{rule_set:($w.rule_sets | map(.tag)),port:[80,443],action:"route",outbound:"gemini-warp"}]
      else [] end) +
     [{domain_suffix:$w.web_domain_suffix,port:[80,443],action:"route",outbound:"gemini-warp"}]) +
    .[4:]
  )
' "$GATEWAY_CONFIG" > "$GATEWAY_CONFIG.next"
install -o root -g browser-gateway -m 0640 "$GATEWAY_CONFIG.next" "$GATEWAY_CONFIG"
rm -f "$GATEWAY_CONFIG.next"

install -d -o root -g root -m 0755 "$EGRESS_DROPIN_DIR"
cat > "$EGRESS_DROPIN.next" <<EOF
[Unit]
After=warp-svc.service
Wants=warp-svc.service

[Service]
ExecStartPost=-/usr/bin/curl --silent --show-error --max-time 20 --proxy socks5h://127.0.0.1:${WARP_PROXY_PORT} https://www.cloudflare.com/cdn-cgi/trace
EOF
install -o root -g root -m 0644 "$EGRESS_DROPIN.next" "$EGRESS_DROPIN"
rm -f "$EGRESS_DROPIN.next"

# Remove only the legacy Browser Gateway plumbing from system sing-box.  Other
# user routes, rule-sets and the existing WireGuard endpoint remain unchanged.
if [[ -e "$LEGACY_GLOBAL_INBOUND" ]]; then
  rm -f "$LEGACY_GLOBAL_INBOUND"
  if [[ -s "$GLOBAL_ROUTE" ]]; then
    jq '
      .route.rules = [.route.rules[] |
        select(((.inbound // []) | index("browser-gateway-warp")) == null) |
        select((.outbound != "wireguard-out") or (((.rule_set // []) | index("gemini")) == null))]
    ' "$GLOBAL_ROUTE" > "$GLOBAL_ROUTE.next"
    install -o root -g root -m 0644 "$GLOBAL_ROUTE.next" "$GLOBAL_ROUTE"
    rm -f "$GLOBAL_ROUTE.next"
  fi
fi

"$GATEWAY_BINARY" check -c "$GATEWAY_CONFIG"
if [[ -x "$GLOBAL_BINARY" && -s "$GLOBAL_ROUTE" ]]; then
  "$GLOBAL_BINARY" check -C "$GLOBAL_CONFIG_DIR"
fi
systemctl daemon-reload
if systemctl is-active --quiet sing-box.service; then
  systemctl restart sing-box.service
fi
systemctl restart browser-gateway-egress.service browser-gateway.service
systemctl is-active --quiet warp-svc.service browser-gateway-egress.service browser-gateway.service

# Test direct Google and WARP-routed Gemini separately.  A status code is not
# required here; a completed TLS/HTTP exchange proves the path is usable.
curl --fail --silent --show-error --max-time 20 --output /dev/null https://www.google.com/
curl --silent --show-error --max-time 20 --output /dev/null \
  --proxy "socks5h://127.0.0.1:${WARP_PROXY_PORT}" https://gemini.google.com/app

trap - ERR
echo "Gemini split routing enabled through Cloudflare WARP MASQUE on 127.0.0.1:${WARP_PROXY_PORT}."
echo "Ordinary Google traffic continues to use the server direct egress."
echo "Backup: $backup"
