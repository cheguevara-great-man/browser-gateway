[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [ValidateRange(1, 65535)]
    [int]$Port = 443,
    [string]$IdentityFile = (Join-Path $HOME '.ssh\browser_gateway_ed25519'),
    [string]$LocalCredentialPath = (Join-Path $HOME '.browser-gateway\deployment.local.json'),
    [string]$UsageAdminCredentialPath = (Join-Path $HOME '.browser-gateway\usage-admin.local.json'),
    [string]$UsageViewerCredentialPath = (Join-Path $HOME '.browser-gateway\usage-viewer.local.json')
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$installer = Join-Path $root 'server\install-h2.sh'
$usageCollector = Join-Path $root 'server\usage_collector.py'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw "Missing installer: $installer" }
if (-not (Test-Path -LiteralPath $usageCollector -PathType Leaf)) { throw "Missing collector: $usageCollector" }
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw "Missing SSH identity: $IdentityFile" }

$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$scp = (Get-Command scp.exe -ErrorAction Stop).Source
$common = @('-i', $IdentityFile, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes')

& $scp @common $installer "root@${Server}:/root/browser-gateway-install-h2.sh"
if ($LASTEXITCODE -ne 0) { throw 'Failed to upload the server installer.' }
& $scp @common $usageCollector "root@${Server}:/root/browser-gateway-usage-collector.py"
if ($LASTEXITCODE -ne 0) { throw 'Failed to upload the token usage collector.' }
& $ssh @common "root@$Server" "chmod 0700 /root/browser-gateway-install-h2.sh && /root/browser-gateway-install-h2.sh '$Server' '$Port'"
if ($LASTEXITCODE -ne 0) { throw 'Server installation failed.' }

$credentialDirectory = Split-Path -Parent $LocalCredentialPath
New-Item -ItemType Directory -Path $credentialDirectory -Force | Out-Null
$usageAdminDirectory = Split-Path -Parent $UsageAdminCredentialPath
New-Item -ItemType Directory -Path $usageAdminDirectory -Force | Out-Null
$usageViewerDirectory = Split-Path -Parent $UsageViewerCredentialPath
New-Item -ItemType Directory -Path $usageViewerDirectory -Force | Out-Null
& $scp @common "root@${Server}:/root/browser-gateway-credentials.json" $LocalCredentialPath
if ($LASTEXITCODE -ne 0) { throw 'Failed to retrieve generated credentials.' }
& $scp @common "root@${Server}:/root/browser-gateway-usage-admin.json" $UsageAdminCredentialPath
if ($LASTEXITCODE -ne 0) { throw 'Failed to retrieve usage administrator credentials.' }
& $scp @common "root@${Server}:/root/browser-gateway-usage-viewer.json" $UsageViewerCredentialPath
if ($LASTEXITCODE -ne 0) { throw 'Failed to retrieve usage viewer credentials.' }
& icacls.exe $LocalCredentialPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the local credential file ACL.' }
& icacls.exe $UsageAdminCredentialPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the usage administrator credential ACL.' }
& icacls.exe $UsageViewerCredentialPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the usage viewer credential ACL.' }

$extensionBootstrap = Join-Path $root 'extension\runtime-config.json'
$gatewayCredential = Get-Content -LiteralPath $LocalCredentialPath -Raw -Encoding UTF8 | ConvertFrom-Json
$usageAdminCredential = Get-Content -LiteralPath $UsageAdminCredentialPath -Raw -Encoding UTF8 | ConvertFrom-Json
$extensionCredential = [ordered]@{
    host = [string]$gatewayCredential.host
    port = [int]$gatewayCredential.port
    username = [string]$gatewayCredential.username
    password = [string]$gatewayCredential.password
    expectedIp = [string]$gatewayCredential.expectedIp
    transport = [string]$gatewayCredential.transport
}
[System.IO.File]::WriteAllText(
    $extensionBootstrap,
    ($extensionCredential | ConvertTo-Json),
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "Browser Gateway HTTP/2 server deployed successfully on TCP $Port." -ForegroundColor Green
Write-Host "Credentials saved locally at: $LocalCredentialPath"
Write-Host "Usage administrator credentials saved locally at: $UsageAdminCredentialPath"
Write-Host "Usage read-only credentials saved locally at: $UsageViewerCredentialPath"
Write-Host "Usage dashboard: $($usageAdminCredential.dashboardUrl)"
Write-Host "Extension bootstrap created at: $extensionBootstrap"
Write-Host 'Credential values were not printed and both local files are excluded from Git.'
