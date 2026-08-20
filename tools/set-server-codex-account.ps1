[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$IdentityFile = (Join-Path $env:USERPROFILE '.ssh\browser_gateway_ed25519'),
    [string]$AuthFile = (Join-Path $env:USERPROFILE '.codex\auth.json')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw "Missing SSH identity: $IdentityFile"
}
if (-not (Test-Path -LiteralPath $AuthFile -PathType Leaf)) {
    throw "Missing Codex login file: $AuthFile"
}

try {
    $auth = Get-Content -LiteralPath $AuthFile -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    throw "The Codex login file is not valid JSON: $AuthFile"
}
if ([string]$auth.auth_mode -ne 'chatgpt') {
    throw 'The selected auth.json is not a ChatGPT Codex login. Sign in with the target ChatGPT account first.'
}
if ([string]::IsNullOrWhiteSpace([string]$auth.tokens.access_token) -or
    [string]::IsNullOrWhiteSpace([string]$auth.tokens.refresh_token)) {
    throw 'The Codex login file does not contain usable access and refresh tokens. Sign in locally first.'
}

$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$scp = (Get-Command scp.exe -ErrorAction Stop).Source
$common = @('-i', $IdentityFile, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes')
$remoteStaging = '/root/browser-gateway-codex-executor-auth.json.next'

& $scp @common $AuthFile "root@${Server}:$remoteStaging"
if ($LASTEXITCODE -ne 0) { throw 'Failed to upload the server Codex credentials.' }

$installCommand = @'
set -eu
install -d -o browser-gateway -g browser-gateway -m 0700 /var/lib/browser-gateway/codex-executor
install -o browser-gateway -g browser-gateway -m 0600 /root/browser-gateway-codex-executor-auth.json.next /var/lib/browser-gateway/codex-executor/auth.json.next
mv -f /var/lib/browser-gateway/codex-executor/auth.json.next /var/lib/browser-gateway/codex-executor/auth.json
rm -f /root/browser-gateway-codex-executor-auth.json.next
systemctl enable --now browser-gateway-codex-executor.service
systemctl is-active --quiet browser-gateway-codex-executor.service
'@ -replace "`r?`n", '; '

& $ssh @common "root@$Server" $installCommand
if ($LASTEXITCODE -ne 0) { throw 'The server rejected the credentials or the Codex executor did not start. Check its systemd logs.' }

Write-Host 'Server-side Codex credentials installed and executor started.' -ForegroundColor Green
Write-Host 'The account file was not printed, copied to Git, or retained in the server staging path.'
