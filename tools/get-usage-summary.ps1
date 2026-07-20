[CmdletBinding()]
param(
    [ValidateRange(1, 366)]
    [int]$Days = 30,
    [string]$StartDate,
    [string]$EndDate,
    [string]$CredentialPath = (Join-Path $HOME '.browser-gateway\usage-admin.local.json')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
    throw "Usage administrator credential file not found: $CredentialPath"
}
$credential = Get-Content -LiteralPath $CredentialPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([bool]$StartDate -xor [bool]$EndDate) {
    throw 'StartDate and EndDate must be supplied together.'
}
if ($StartDate) {
    $start = [datetime]::ParseExact($StartDate, 'yyyy-MM-dd', $null).ToString('yyyy-MM-dd')
    $end = [datetime]::ParseExact($EndDate, 'yyyy-MM-dd', $null).ToString('yyyy-MM-dd')
    if ([datetime]$start -gt [datetime]$end) { throw 'StartDate must not be after EndDate.' }
    $uri = "$($credential.summaryUrl)?start=$start&end=$end"
} else {
    $uri = "$($credential.summaryUrl)?days=$Days"
}
$headers = @{ Authorization = "Bearer $($credential.adminToken)" }
$summary = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get

$summary.machines |
    Select-Object machine_name, requests, input_tokens, cached_input_tokens, output_tokens,
        total_tokens, estimated_credits, deviation_percent, catch_up_to_highest, last_seen |
    Format-Table -AutoSize
Write-Host "Total machines: $($summary.totals.machines)"
Write-Host "Total requests: $($summary.totals.requests)"
Write-Host "Total tokens:   $($summary.totals.total_tokens)"
Write-Host "Estimated credits: $($summary.totals.estimated_credits)"
if ($summary.totals.unrated_tokens -gt 0) {
    Write-Warning "$($summary.totals.unrated_tokens) tokens use an unknown model rate and are not included in estimated credits."
}
