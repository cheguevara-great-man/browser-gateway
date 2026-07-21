[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$IdentityFile = (Join-Path $HOME '.ssh\browser_gateway_ed25519')
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script = Join-Path $root 'server\configure-gemini-warp.sh'
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Missing configuration script: $script" }
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw "Missing SSH identity: $IdentityFile" }

$common = @('-i', $IdentityFile, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10')
& scp.exe @common $script "root@${Server}:/root/configure-gemini-warp.sh"
if ($LASTEXITCODE -ne 0) { throw 'Failed to upload the Gemini WARP configuration script.' }

& ssh.exe @common "root@$Server" 'chmod 0700 /root/configure-gemini-warp.sh && /root/configure-gemini-warp.sh'
if ($LASTEXITCODE -ne 0) { throw 'Gemini WARP split-routing configuration failed.' }

Write-Host 'Gemini WARP split routing is active. Other Browser Gateway traffic still uses the server direct egress.' -ForegroundColor Green
