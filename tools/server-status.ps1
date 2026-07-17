param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [ValidateRange(1, 65535)]
    [int]$Port = 443,
    [string]$IdentityFile = (Join-Path $HOME '.ssh\browser_gateway_ed25519')
)

$ErrorActionPreference = 'Stop'
$commands = @'
systemctl is-active browser-gateway.service
systemctl is-enabled browser-gateway.service
systemctl is-active browser-gateway-egress.service
systemctl is-active browser-gateway-usage.service
systemctl is-active browser-gateway-health.timer
systemctl is-active browser-gateway-cert-renew.timer
ss -ltnp 'sport = :__PORT__'
ss -ltnp 'sport = :9443'
ss -ltnp 'sport = :19443'
openssl x509 -in /etc/browser-gateway/tls/fullchain.pem -noout -subject -issuer -dates -ext subjectAltName
journalctl -u browser-gateway.service -n 20 --no-pager
journalctl -u browser-gateway-usage.service -n 20 --no-pager
'@
$commands = $commands.Replace('__PORT__', [string]$Port)
& ssh.exe -i $IdentityFile -o BatchMode=yes -o StrictHostKeyChecking=yes "root@$Server" $commands
if ($LASTEXITCODE -ne 0) { throw 'Server health check failed.' }
