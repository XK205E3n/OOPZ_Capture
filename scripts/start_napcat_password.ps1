param(
    [Parameter(Mandatory = $true)]
    [string]$NapCatDirectory
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$startScript = Join-Path $NapCatDirectory 'start-napcat.bat'
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "NapCat start script not found: $startScript"
}

$form = New-Object System.Windows.Forms.Form
$form.Text = 'OOPZ Robot QQ Login'
$form.Size = New-Object System.Drawing.Size(390, 205)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$accountLabel = New-Object System.Windows.Forms.Label
$accountLabel.Text = 'QQ account'
$accountLabel.Location = New-Object System.Drawing.Point(25, 25)
$accountLabel.AutoSize = $true
$form.Controls.Add($accountLabel)

$accountBox = New-Object System.Windows.Forms.TextBox
$accountBox.Location = New-Object System.Drawing.Point(125, 20)
$accountBox.Size = New-Object System.Drawing.Size(220, 25)
$form.Controls.Add($accountBox)

$passwordLabel = New-Object System.Windows.Forms.Label
$passwordLabel.Text = 'Password'
$passwordLabel.Location = New-Object System.Drawing.Point(25, 70)
$passwordLabel.AutoSize = $true
$form.Controls.Add($passwordLabel)

$passwordBox = New-Object System.Windows.Forms.TextBox
$passwordBox.Location = New-Object System.Drawing.Point(125, 65)
$passwordBox.Size = New-Object System.Drawing.Size(220, 25)
$passwordBox.UseSystemPasswordChar = $true
$form.Controls.Add($passwordBox)

$startButton = New-Object System.Windows.Forms.Button
$startButton.Text = 'Login and start'
$startButton.Location = New-Object System.Drawing.Point(125, 115)
$startButton.Size = New-Object System.Drawing.Size(120, 30)
$startButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.AcceptButton = $startButton
$form.Controls.Add($startButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = 'Cancel'
$cancelButton.Location = New-Object System.Drawing.Point(255, 115)
$cancelButton.Size = New-Object System.Drawing.Size(90, 30)
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.CancelButton = $cancelButton
$form.Controls.Add($cancelButton)

if ($form.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
    exit 2
}
$account = $accountBox.Text.Trim()
if ($account -notmatch '^\d{5,20}$') {
    throw 'QQ account must contain 5 to 20 digits.'
}
if ([string]::IsNullOrWhiteSpace($passwordBox.Text)) {
    throw 'Password must not be empty.'
}

$secure = ConvertTo-SecureString $passwordBox.Text -AsPlainText -Force
$passwordBox.Clear()
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $stateRoot = Join-Path $projectRoot 'controller_state'
    New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
    Set-Content -LiteralPath (Join-Path $stateRoot 'napcat_account.txt') -Value $account -NoNewline -Encoding ascii
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $env:NAPCAT_QUICK_ACCOUNT = $account
    $env:NAPCAT_QUICK_PASSWORD = $plainPassword

    # Preserve only a DPAPI-protected login digest for unattended recovery.
    # This is bound to the current Windows account and computer; neither the
    # plaintext password nor a plaintext digest is written to disk.
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $digestBytes = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($plainPassword))
        $digest = -join ($digestBytes | ForEach-Object { $_.ToString('x2') })
        $secureDigest = ConvertTo-SecureString $digest -AsPlainText -Force
        $protectedDigest = ConvertFrom-SecureString $secureDigest
        Set-Content -LiteralPath (Join-Path $stateRoot 'napcat_password_md5.dpapi') `
            -Value $protectedDigest -NoNewline -Encoding ascii
    }
    finally {
        $md5.Dispose()
        $plainPassword = $null
        $digest = $null
    }
    # Do not hide QQ. If Tencent requires a CAPTCHA or a device verification,
    # the user must be able to interact with its official verification window.
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', ('"' + $startScript + '"')) -WorkingDirectory $NapCatDirectory -WindowStyle Normal
    Start-Sleep -Seconds 2
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class OopzWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@
    $qq = Get-Process -Name 'QQ' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($NapCatDirectory, [System.StringComparison]::OrdinalIgnoreCase) } |
        Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } |
        Select-Object -First 1
    if ($qq) {
        [void][OopzWindow]::ShowWindowAsync($qq.MainWindowHandle, 9)
        [void][OopzWindow]::SetForegroundWindow($qq.MainWindowHandle)
    }
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    Remove-Item Env:NAPCAT_QUICK_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:NAPCAT_QUICK_ACCOUNT -ErrorAction SilentlyContinue
}
