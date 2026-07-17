[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$IdentityFile = (Join-Path $HOME '.ssh\browser_gateway_ed25519')
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$configuration = Join-Path $root 'server\ssh-hardening.conf'
if (-not (Test-Path -LiteralPath $configuration -PathType Leaf)) { throw "Missing SSH configuration: $configuration" }
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw "Missing SSH identity: $IdentityFile" }

$common = @('-i', $IdentityFile, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10')

# Do not disable password login until a fresh key-only connection succeeds.
& ssh.exe @common "root@$Server" 'true'
if ($LASTEXITCODE -ne 0) { throw 'SSH key login verification failed; no hardening changes were made.' }

& scp.exe @common $configuration "root@${Server}:/root/browser-gateway-ssh-hardening.conf"
if ($LASTEXITCODE -ne 0) { throw 'Failed to upload the SSH hardening configuration.' }

$remote = @'
set -e
install -o root -g root -m 0644 /root/browser-gateway-ssh-hardening.conf /etc/ssh/sshd_config.d/90-browser-gateway-hardening.conf
/usr/sbin/sshd -t
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends unattended-upgrades
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer
systemctl reload ssh.service
'@
& ssh.exe @common "root@$Server" $remote
if ($LASTEXITCODE -ne 0) { throw 'Server hardening failed.' }

Start-Sleep -Seconds 2
& ssh.exe @common "root@$Server" "sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) '"
if ($LASTEXITCODE -ne 0) { throw 'Key-only SSH reconnect failed after hardening.' }

Write-Host 'SSH password login disabled; key login and unattended security updates are active.' -ForegroundColor Green
