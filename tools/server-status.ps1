param(
    [string]$Server = '38.207.167.51',
    [string]$IdentityFile = (Join-Path $HOME '.ssh\browser_gateway_ed25519')
)

$ErrorActionPreference = 'Stop'
& ssh.exe -i $IdentityFile -o BatchMode=yes -o StrictHostKeyChecking=yes "root@$Server" @'
systemctl is-active browser-gateway.service
systemctl is-enabled browser-gateway.service
systemctl is-active browser-gateway-cert-renew.timer
ss -ltnp 'sport = :443'
openssl x509 -in /etc/browser-gateway/tls/fullchain.pem -noout -subject -issuer -dates -ext subjectAltName
journalctl -u browser-gateway.service -n 20 --no-pager
'@
if ($LASTEXITCODE -ne 0) { throw 'Server health check failed.' }
