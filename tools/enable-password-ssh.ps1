[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$IdentityFile = (Join-Path $HOME '.ssh\browser_gateway_ed25519')
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$configuration = Join-Path $root 'server\ssh-password-access.conf'
if (-not (Test-Path -LiteralPath $configuration -PathType Leaf)) { throw "Missing SSH configuration: $configuration" }
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw "Missing SSH identity: $IdentityFile" }

$common = @('-i', $IdentityFile, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10')
& ssh.exe @common "root@$Server" 'true'
if ($LASTEXITCODE -ne 0) { throw 'SSH key login verification failed; no SSH changes were made.' }

& scp.exe @common $configuration "root@${Server}:/root/browser-gateway-ssh-password-access.conf"
if ($LASTEXITCODE -ne 0) { throw 'Failed to upload the SSH configuration.' }

$remote = @'
set -e
status="$(passwd -S root | awk '{print $2}')"
test "$status" = "P" || { echo "root password is not set or is locked" >&2; exit 1; }
install -d -o root -g root -m 0700 /root/browser-gateway-backups
if test -f /etc/ssh/sshd_config.d/90-browser-gateway-hardening.conf; then
  cp -a /etc/ssh/sshd_config.d/90-browser-gateway-hardening.conf "/root/browser-gateway-backups/90-browser-gateway-hardening.conf.$(date -u +%Y%m%dT%H%M%SZ)"
fi
install -o root -g root -m 0644 /root/browser-gateway-ssh-password-access.conf /etc/ssh/sshd_config.d/90-browser-gateway-hardening.conf
/usr/sbin/sshd -t
systemctl reload ssh.service
/usr/sbin/sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) '
'@
& ssh.exe @common "root@$Server" $remote
if ($LASTEXITCODE -ne 0) { throw 'Enabling SSH password access failed.' }

Start-Sleep -Seconds 1
& ssh.exe @common "root@$Server" 'true'
if ($LASTEXITCODE -ne 0) { throw 'SSH key reconnect failed after changing the SSH configuration.' }

Write-Host 'Root password SSH access is enabled; SSH key access remains enabled.' -ForegroundColor Green
