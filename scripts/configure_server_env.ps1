param(
    [string]$EnvPath = 'C:\OOPZ\shared\config\.env',
    [string]$TemplatePath
)
$ErrorActionPreference = 'Stop'

function Read-OopzConfigValue {
    param([string]$Key)
    if ($Key -match 'PASSWORD|PHONE|API_KEY|SECRET|TOKEN') {
        $secure = Read-Host $Key -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer); $secure.Dispose() }
    }
    return Read-Host $Key
}

function Initialize-OopzServerEnv {
    param([string]$Path, [string]$Template)
    if (-not (Test-Path -LiteralPath $Path)) {
        if (-not $Template -or -not (Test-Path -LiteralPath $Template -PathType Leaf)) { throw 'A verified .env.example template is required for the first setup.' }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
        Copy-Item -LiteralPath $Template -Destination $Path
    }
    $keys = @('OOPZ_LOGIN_PHONE','OOPZ_LOGIN_PASSWORD','ANALYZER_PROVIDER','ANALYZER_API_KEY','ANALYZER_BASE_URL','ANALYZER_MODEL','ANALYZER_TIMEOUT_SECONDS','ANALYZER_MAX_RETRIES','ANALYZER_MIN_INTERVAL_SECONDS','ANALYZER_MAX_TOKENS','ANALYZER_THINKING_MAX_TOKENS','ANALYZER_THINKING_MODE','ANALYZER_JSON_MODE')
    foreach ($key in $keys) {
        $lines = [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)
        $pattern = '^\s*' + [regex]::Escape($key) + '\s*=(.*)$'
        $existing = @($lines | Where-Object { $_ -match $pattern })
        if ($existing.Count -gt 1) { throw "Duplicate configuration key: $key. Inspect the file locally before continuing." }
        if ($existing.Count -eq 1) {
            $null = $existing[0] -match $pattern
            $current = $Matches[1].Trim()
            if ($current -and $current -notin @('""', "''")) { Write-Host "KEEP $key"; continue }
        }
        $value = Read-OopzConfigValue $key
        if ([string]::IsNullOrWhiteSpace($value) -or $value -match '[\r\n]') { throw "Missing or multiline value for $key; nothing was written for this key." }
        $replacement = $key + '="' + $value + '"'
        if ($existing.Count) { $lines = @($lines | ForEach-Object { if ($_ -match $pattern) { $replacement } else { $_ } }) }
        else { $lines = @($lines) + $replacement }
        # Write in place: replacing the file would break release/shared hard links.
        [IO.File]::WriteAllLines($Path, [string[]]$lines, [Text.UTF8Encoding]::new($false))
        $value = $null; $replacement = $null
        Write-Host "SAVED $key"
    }
    Write-Host 'Required OOPZ/API fields are present. Run the one-click Feishu setup next; do not print or share the .env contents.'
}

if ($MyInvocation.InvocationName -ne '.') { Initialize-OopzServerEnv $EnvPath $TemplatePath }
