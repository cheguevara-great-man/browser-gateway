[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [ValidateRange(1, 65535)]
    [int]$Port = 443,
    [string]$IdentityFile = (Join-Path $HOME '.ssh\browser_gateway_ed25519'),
    [string]$LocalCredentialPath = (Join-Path $HOME '.browser-gateway\deployment.local.json')
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$installer = Join-Path $root 'server\install-h2.sh'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw "Missing installer: $installer" }
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw "Missing SSH identity: $IdentityFile" }

$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$scp = (Get-Command scp.exe -ErrorAction Stop).Source
$common = @('-i', $IdentityFile, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes')

& $scp @common $installer "root@${Server}:/root/browser-gateway-install-h2.sh"
if ($LASTEXITCODE -ne 0) { throw 'Failed to upload the server installer.' }
& $ssh @common "root@$Server" "chmod 0700 /root/browser-gateway-install-h2.sh && /root/browser-gateway-install-h2.sh '$Server' '$Port'"
if ($LASTEXITCODE -ne 0) { throw 'Server installation failed.' }

$credentialDirectory = Split-Path -Parent $LocalCredentialPath
New-Item -ItemType Directory -Path $credentialDirectory -Force | Out-Null
& $scp @common "root@${Server}:/root/browser-gateway-credentials.json" $LocalCredentialPath
if ($LASTEXITCODE -ne 0) { throw 'Failed to retrieve generated credentials.' }
& icacls.exe $LocalCredentialPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the local credential file ACL.' }

$extensionBootstrap = Join-Path $root 'extension\runtime-config.json'
Copy-Item -LiteralPath $LocalCredentialPath -Destination $extensionBootstrap -Force

Write-Host "Browser Gateway HTTP/2 server deployed successfully on TCP $Port." -ForegroundColor Green
Write-Host "Credentials saved locally at: $LocalCredentialPath"
Write-Host "Extension bootstrap created at: $extensionBootstrap"
Write-Host 'Credential values were not printed and both local files are excluded from Git.'
