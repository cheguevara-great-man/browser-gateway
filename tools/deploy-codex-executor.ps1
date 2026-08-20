[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [ValidateRange(1024, 65535)]
    [int]$Port = 9444,
    [string]$IdentityFile = (Join-Path $env:USERPROFILE '.ssh\browser_gateway_ed25519')
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$installer = Join-Path $root 'server\install-codex-executor.sh'
$executor = Join-Path $root 'server\codex_executor.py'
$credentials = Join-Path $root 'server\codex_credentials.py'
$usageCollector = Join-Path $root 'server\usage_collector.py'
foreach ($path in @($installer, $executor, $credentials, $usageCollector, $IdentityFile)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing required file: $path" }
}

$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$scp = (Get-Command scp.exe -ErrorAction Stop).Source
$common = @('-i', $IdentityFile, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes')
foreach ($pair in @(
    @($installer, '/root/browser-gateway-install-codex-executor.sh'),
    @($executor, '/root/browser-gateway-codex-executor.py'),
    @($credentials, '/root/browser-gateway-codex-credentials.py'),
    @($usageCollector, '/root/browser-gateway-usage-collector.py')
)) {
    & $scp @common $pair[0] "root@${Server}:$($pair[1])"
    if ($LASTEXITCODE -ne 0) { throw "Failed to upload $($pair[0])." }
}

& $ssh @common "root@$Server" "chmod 0700 /root/browser-gateway-install-codex-executor.sh && /root/browser-gateway-install-codex-executor.sh '$Server' '$Port'"
if ($LASTEXITCODE -ne 0) { throw 'Isolated Codex executor installation failed.' }

Write-Host "Isolated Codex executor installed on TCP $Port." -ForegroundColor Green
Write-Host 'It did not reload or restart the existing Browser Gateway proxy, usage dashboard, GOST, sing-box, or Nginx.'
Write-Host 'Run set-server-codex-account.ps1 separately to upload the server account and start only the new executor.'
