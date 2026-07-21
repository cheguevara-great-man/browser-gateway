#!/usr/bin/env bash
set -euo pipefail

GLOBAL_CONFIG_DIR="${GLOBAL_CONFIG_DIR:-/etc/sing-box/conf}"
GLOBAL_ROUTE="$GLOBAL_CONFIG_DIR/route.json"
GLOBAL_INBOUND="$GLOBAL_CONFIG_DIR/browser-gateway-warp.json"
GLOBAL_BINARY="${GLOBAL_BINARY:-/etc/sing-box/sing-box}"
WARP_ENDPOINT_TAG="${WARP_ENDPOINT_TAG:-wireguard-out}"
GEMINI_RULE_SET_TAG="${GEMINI_RULE_SET_TAG:-gemini}"
GOOGLE_RULE_SET_TAG="${GOOGLE_RULE_SET_TAG:-google}"
WARP_PROXY_PORT="${WARP_PROXY_PORT:-18089}"
GATEWAY_CONFIG_ROOT="${GATEWAY_CONFIG_ROOT:-/etc/browser-gateway}"
GATEWAY_CONFIG="$GATEWAY_CONFIG_ROOT/egress.json"
GATEWAY_BINARY="${GATEWAY_BINARY:-/opt/browser-gateway/bin/sing-box}"
SETTINGS="$GATEWAY_CONFIG_ROOT/gemini-warp.json"
GEMINI_RULE_SET="$GATEWAY_CONFIG_ROOT/gemini.srs"
GOOGLE_RULE_SET="$GATEWAY_CONFIG_ROOT/google.srs"
BACKUP_ROOT="/root/browser-gateway-backups"
EGRESS_DROPIN_DIR="/etc/systemd/system/browser-gateway-egress.service.d"
EGRESS_DROPIN="$EGRESS_DROPIN_DIR/gemini-warp.conf"

fail() { echo "gemini-warp: $*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || fail "run as root"
[[ -x "$GLOBAL_BINARY" ]] || fail "system sing-box binary not found at $GLOBAL_BINARY"
[[ -x "$GATEWAY_BINARY" ]] || fail "Browser Gateway sing-box binary not found at $GATEWAY_BINARY"
[[ -s "$GLOBAL_ROUTE" ]] || fail "system sing-box route config not found at $GLOBAL_ROUTE"
[[ -s "$GATEWAY_CONFIG" ]] || fail "Browser Gateway egress config not found at $GATEWAY_CONFIG"
[[ "$WARP_PROXY_PORT" =~ ^[0-9]+$ ]] || fail "WARP proxy port must be numeric"

endpoint_found=false
for file in "$GLOBAL_CONFIG_DIR"/*.json; do
  if jq -e --arg tag "$WARP_ENDPOINT_TAG" '.endpoints[]? | select(.tag == $tag and .type == "wireguard")' "$file" >/dev/null; then
    endpoint_found=true
    break
  fi
done
[[ "$endpoint_found" == true ]] || fail "WireGuard endpoint '$WARP_ENDPOINT_TAG' was not found"
for tag in "$GEMINI_RULE_SET_TAG" "$GOOGLE_RULE_SET_TAG"; do
  jq -e --arg tag "$tag" '.route.rule_set[]? | select(.tag == $tag)' "$GLOBAL_ROUTE" >/dev/null || \
    fail "required rule-set '$tag' was not found"
done
gemini_rule_set_url="$(jq -er --arg tag "$GEMINI_RULE_SET_TAG" '.route.rule_set[] | select(.tag == $tag) | .url' "$GLOBAL_ROUTE")"
google_rule_set_url="$(jq -er --arg tag "$GOOGLE_RULE_SET_TAG" '.route.rule_set[] | select(.tag == $tag) | .url' "$GLOBAL_ROUTE")"

install -d -o root -g root -m 0700 "$BACKUP_ROOT"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$BACKUP_ROOT/gemini-warp-$stamp"
install -d -o root -g root -m 0700 "$backup"
cp -a "$GLOBAL_ROUTE" "$GATEWAY_CONFIG" "$backup/"
[[ -e "$GLOBAL_INBOUND" ]] && cp -a "$GLOBAL_INBOUND" "$backup/global-inbound.json"
[[ -e "$SETTINGS" ]] && cp -a "$SETTINGS" "$backup/gemini-warp.json"
[[ -e "$GEMINI_RULE_SET" ]] && cp -a "$GEMINI_RULE_SET" "$backup/gemini.srs"
[[ -e "$GOOGLE_RULE_SET" ]] && cp -a "$GOOGLE_RULE_SET" "$backup/google.srs"
[[ -e "$EGRESS_DROPIN" ]] && cp -a "$EGRESS_DROPIN" "$backup/egress-dropin.conf"

rollback() {
  cp -a "$backup/route.json" "$GLOBAL_ROUTE"
  cp -a "$backup/egress.json" "$GATEWAY_CONFIG"
  if [[ -e "$backup/global-inbound.json" ]]; then
    cp -a "$backup/global-inbound.json" "$GLOBAL_INBOUND"
  else
    rm -f "$GLOBAL_INBOUND"
  fi
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
  if [[ -e "$backup/google.srs" ]]; then
    cp -a "$backup/google.srs" "$GOOGLE_RULE_SET"
  else
    rm -f "$GOOGLE_RULE_SET"
  fi
  if [[ -e "$backup/egress-dropin.conf" ]]; then
    install -d -o root -g root -m 0755 "$EGRESS_DROPIN_DIR"
    cp -a "$backup/egress-dropin.conf" "$EGRESS_DROPIN"
  else
    rm -f "$EGRESS_DROPIN"
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl restart sing-box.service browser-gateway-egress.service >/dev/null 2>&1 || true
}
trap 'rollback' ERR

jq -n --argjson port "$WARP_PROXY_PORT" '{
  inbounds:[{type:"mixed",tag:"browser-gateway-warp",listen:"127.0.0.1",listen_port:$port}]
}' > "$GLOBAL_INBOUND.next"
install -o root -g root -m 0644 "$GLOBAL_INBOUND.next" "$GLOBAL_INBOUND"
rm -f "$GLOBAL_INBOUND.next"

jq --arg inbound "browser-gateway-warp" --arg warp "$WARP_ENDPOINT_TAG" \
  --arg gemini "$GEMINI_RULE_SET_TAG" --arg google "$GOOGLE_RULE_SET_TAG" '
  .route.rules = (
    [
      {inbound:[$inbound],action:"route",outbound:$warp},
      {rule_set:[$gemini,$google],action:"route",outbound:$warp}
    ] +
    [
      .route.rules[] |
      select(((.inbound // []) | index($inbound)) == null) |
      select(((((.rule_set // []) | index($gemini)) == null) and (((.rule_set // []) | index($google)) == null)) or (.outbound != $warp))
    ]
  )
' "$GLOBAL_ROUTE" > "$GLOBAL_ROUTE.next"
install -o root -g root -m 0644 "$GLOBAL_ROUTE.next" "$GLOBAL_ROUTE"
rm -f "$GLOBAL_ROUTE.next"

jq -n --argjson port "$WARP_PROXY_PORT" --arg gemini_path "$GEMINI_RULE_SET" --arg google_path "$GOOGLE_RULE_SET" '{
  enabled:true,
  proxy_port:$port,
  rule_sets:[
    {tag:"gemini-warp-gemini",path:$gemini_path},
    {tag:"gemini-warp-google",path:$google_path}
  ]
}' > "$SETTINGS.next"
install -o root -g browser-gateway -m 0640 "$SETTINGS.next" "$SETTINGS"
rm -f "$SETTINGS.next"

curl --fail --location --silent --show-error --retry 3 --retry-all-errors \
  "$gemini_rule_set_url" --output "$GEMINI_RULE_SET.next"
install -o root -g browser-gateway -m 0640 "$GEMINI_RULE_SET.next" "$GEMINI_RULE_SET"
rm -f "$GEMINI_RULE_SET.next"
curl --fail --location --silent --show-error --retry 3 --retry-all-errors \
  "$google_rule_set_url" --output "$GOOGLE_RULE_SET.next"
install -o root -g browser-gateway -m 0640 "$GOOGLE_RULE_SET.next" "$GOOGLE_RULE_SET"
rm -f "$GOOGLE_RULE_SET.next"

install -d -o root -g root -m 0755 "$EGRESS_DROPIN_DIR"
cat > "$EGRESS_DROPIN.next" <<'EOF'
[Unit]
After=sing-box.service
Wants=sing-box.service

[Service]
ExecStartPost=-/usr/bin/curl --silent --show-error --max-time 15 --proxy socks5h://127.0.0.1:18089 https://www.cloudflare.com/cdn-cgi/trace
EOF
install -o root -g root -m 0644 "$EGRESS_DROPIN.next" "$EGRESS_DROPIN"
rm -f "$EGRESS_DROPIN.next"

jq --slurpfile warp "$SETTINGS" '
  ($warp[0]) as $w |
  .outbounds = ([.outbounds[] | select(.tag != "gemini-warp")] +
    [{type:"socks",tag:"gemini-warp",server:"127.0.0.1",server_port:$w.proxy_port,version:"5"}]) |
  .route.rule_set = ([.route.rule_set[]? | select(.tag | startswith("gemini-warp-") | not)] +
    ($w.rule_sets | map({type:"local",tag:.tag,format:"binary",path:.path}))) |
  .route.rules = (
    [.route.rules[] | select(.outbound != "gemini-warp")] |
    .[0:4] +
    [{rule_set:($w.rule_sets | map(.tag)),port:[80,443],action:"route",outbound:"gemini-warp"}] +
    .[4:]
  )
' "$GATEWAY_CONFIG" > "$GATEWAY_CONFIG.next"
install -o root -g browser-gateway -m 0640 "$GATEWAY_CONFIG.next" "$GATEWAY_CONFIG"
rm -f "$GATEWAY_CONFIG.next"

"$GLOBAL_BINARY" check -C "$GLOBAL_CONFIG_DIR"
"$GATEWAY_BINARY" check -c "$GATEWAY_CONFIG"
systemctl daemon-reload
systemctl restart sing-box.service
for _ in {1..20}; do
  ss -ltnH "sport = :${WARP_PROXY_PORT}" | grep -q . && break
  sleep 0.25
done
ss -ltnH "sport = :${WARP_PROXY_PORT}" | grep -q . || fail "local WARP proxy did not start"
systemctl restart browser-gateway-egress.service browser-gateway.service
systemctl is-active --quiet sing-box.service browser-gateway-egress.service browser-gateway.service
trap - ERR

echo "Gemini split routing enabled through system sing-box WARP on 127.0.0.1:${WARP_PROXY_PORT}."
echo "Backup: $backup"
