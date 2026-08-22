param(
    [Parameter(Mandatory = $true)]
    [string]$NapCatDirectory
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$accountFile = Join-Path $projectRoot 'controller_state\napcat_account.txt'
$credentialFile = Join-Path $projectRoot 'controller_state\napcat_password_md5.dpapi'
$startScript = Join-Path $NapCatDirectory 'start-napcat.bat'
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "NapCat start script not found: $startScript"
}

$account = if (Test-Path -LiteralPath $accountFile -PathType Leaf) {
    (Get-Content -LiteralPath $accountFile -Raw).Trim()
} else {
    $configDirectory = Join-Path $NapCatDirectory 'config'
    $candidates = @(Get-ChildItem -LiteralPath $configDirectory -Filter 'onebot11_*.json' -File -ErrorAction SilentlyContinue |
        ForEach-Object { $_.BaseName.Substring('onebot11_'.Length) } |
        Where-Object { $_ -match '^\d{5,20}$' })
    if ($candidates.Count -eq 1) { $candidates[0] } else { '' }
}
if ($account -notmatch '^\d{5,20}$') {
    throw 'Saved NapCat account is unavailable. Run the normal full-stack launcher once to enter the QQ account and password.'
}

$env:NAPCAT_QUICK_ACCOUNT = $account
$credentialPointer = [IntPtr]::Zero
try {
    if (Test-Path -LiteralPath $credentialFile -PathType Leaf) {
        try {
            # The file contains a DPAPI-protected MD5 password digest.  It can
            # only be decrypted by the same Windows user on this computer.
            $protected = (Get-Content -LiteralPath $credentialFile -Raw).Trim()
            $secureDigest = ConvertTo-SecureString $protected
            $credentialPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureDigest)
            $digest = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($credentialPointer)
            if ($digest -notmatch '^[0-9a-fA-F]{32}$') {
                throw 'decrypted password digest has an invalid format'
            }
            $env:NAPCAT_QUICK_PASSWORD_MD5 = $digest.ToLowerInvariant()
            Write-Host 'NapCat 将使用：历史会话快速登录；失效时自动尝试加密凭据回退；仍受风控时才需要扫码。'
        }
        catch {
            Write-Warning "NapCat 加密回退凭据不可用，将仅尝试历史会话：$($_.Exception.Message)"
        }
    }
    else {
        Write-Warning '未配置 NapCat 加密回退凭据；历史会话失效时仍会要求扫码。请运行 配置NapCat自动登录.bat。'
    }
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', ('"' + $startScript + '"')) -WorkingDirectory $NapCatDirectory -WindowStyle Normal
}
finally {
    if ($credentialPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($credentialPointer)
    }
    Remove-Item Env:NAPCAT_QUICK_PASSWORD_MD5 -ErrorAction SilentlyContinue
    Remove-Item Env:NAPCAT_QUICK_ACCOUNT -ErrorAction SilentlyContinue
}
