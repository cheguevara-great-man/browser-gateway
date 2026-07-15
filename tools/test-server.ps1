param(
    [string]$CredentialPath = (Join-Path $HOME '.browser-gateway\deployment.local.json')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
    throw "Credential file not found: $CredentialPath"
}
$credential = Get-Content -LiteralPath $CredentialPath -Raw -Encoding UTF8 | ConvertFrom-Json
$proxy = "https://$($credential.host):$($credential.port)"
$authentication = "$($credential.username):$($credential.password)"

function Test-CurlFails([string[]]$Arguments) {
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & curl.exe @Arguments 2>$null | Out-Null
        return $LASTEXITCODE -ne 0
    } finally {
        $ErrorActionPreference = $savedPreference
    }
}

$egress = & curl.exe --silent --show-error --fail `
    --proxy $proxy --proxy-user $authentication --max-time 20 `
    https://api.ipify.org
if ($LASTEXITCODE -ne 0) { throw 'Authenticated HTTPS proxy request failed.' }
if ($egress.Trim() -ne $credential.expectedIp) {
    throw "Unexpected proxy egress: $($egress.Trim())"
}

$invalidAuthentication = @('--silent', '--show-error', '--fail', '--output', 'NUL',
    '--proxy', $proxy, '--proxy-user', 'invalid:invalid', '--max-time', '12', 'https://example.com')
if (-not (Test-CurlFails $invalidAuthentication)) { throw 'The proxy accepted invalid credentials.' }

$privateDestination = @('--silent', '--show-error', '--fail', '--output', 'NUL', '--noproxy', '',
    '--proxy', $proxy, '--proxy-user', $authentication, '--max-time', '12', 'http://127.0.0.1/')
if (-not (Test-CurlFails $privateDestination)) { throw 'The proxy allowed a private destination.' }

$disallowedPort = @('--silent', '--show-error', '--fail', '--output', 'NUL',
    '--proxy', $proxy, '--proxy-user', $authentication, '--max-time', '12', 'https://example.com:22')
if (-not (Test-CurlFails $disallowedPort)) { throw 'The proxy allowed a non-Web destination port.' }

Write-Host "Server security tests: OK (egress $($egress.Trim()))" -ForegroundColor Green
