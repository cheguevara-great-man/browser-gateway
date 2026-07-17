[CmdletBinding()]
param(
    [ValidateRange(1, 366)]
    [int]$Days = 30,
    [string]$CredentialPath = (Join-Path $HOME '.browser-gateway\usage-admin.local.json')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
    throw "Usage administrator credential file not found: $CredentialPath"
}
$credential = Get-Content -LiteralPath $CredentialPath -Raw -Encoding UTF8 | ConvertFrom-Json
$uri = "$($credential.summaryUrl)?days=$Days"
$headers = @{ Authorization = "Bearer $($credential.adminToken)" }
$summary = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get

$summary.machines |
    Select-Object machine_name, requests, input_tokens, cached_input_tokens, output_tokens, total_tokens, last_seen |
    Format-Table -AutoSize
Write-Host "Total machines: $($summary.totals.machines)"
Write-Host "Total requests: $($summary.totals.requests)"
Write-Host "Total tokens:   $($summary.totals.total_tokens)"
