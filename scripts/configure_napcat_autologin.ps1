param()

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$projectRoot = Split-Path -Parent $PSScriptRoot
$stateRoot = Join-Path $projectRoot 'controller_state'
$accountFile = Join-Path $stateRoot 'napcat_account.txt'
$credentialFile = Join-Path $stateRoot 'napcat_password_md5.dpapi'

$form = New-Object System.Windows.Forms.Form
$form.Text = '配置 NapCat 自动登录回退'
$form.Size = New-Object System.Drawing.Size(430, 235)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$hint = New-Object System.Windows.Forms.Label
$hint.Text = '仅保存 Windows 当前用户加密后的密码摘要，不保存明文密码。'
$hint.Location = New-Object System.Drawing.Point(20, 15)
$hint.Size = New-Object System.Drawing.Size(380, 35)
$form.Controls.Add($hint)

$accountLabel = New-Object System.Windows.Forms.Label
$accountLabel.Text = '机器人 QQ号'
$accountLabel.Location = New-Object System.Drawing.Point(20, 65)
$accountLabel.AutoSize = $true
$form.Controls.Add($accountLabel)

$accountBox = New-Object System.Windows.Forms.TextBox
$accountBox.Location = New-Object System.Drawing.Point(130, 60)
$accountBox.Size = New-Object System.Drawing.Size(250, 25)
if (Test-Path -LiteralPath $accountFile -PathType Leaf) {
    $accountBox.Text = (Get-Content -LiteralPath $accountFile -Raw).Trim()
}
$form.Controls.Add($accountBox)

$passwordLabel = New-Object System.Windows.Forms.Label
$passwordLabel.Text = 'QQ 密码'
$passwordLabel.Location = New-Object System.Drawing.Point(20, 105)
$passwordLabel.AutoSize = $true
$form.Controls.Add($passwordLabel)

$passwordBox = New-Object System.Windows.Forms.TextBox
$passwordBox.Location = New-Object System.Drawing.Point(130, 100)
$passwordBox.Size = New-Object System.Drawing.Size(250, 25)
$passwordBox.UseSystemPasswordChar = $true
$form.Controls.Add($passwordBox)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Text = '加密保存'
$saveButton.Location = New-Object System.Drawing.Point(190, 145)
$saveButton.Size = New-Object System.Drawing.Size(90, 30)
$saveButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.AcceptButton = $saveButton
$form.Controls.Add($saveButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = '取消'
$cancelButton.Location = New-Object System.Drawing.Point(290, 145)
$cancelButton.Size = New-Object System.Drawing.Size(90, 30)
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.CancelButton = $cancelButton
$form.Controls.Add($cancelButton)

if ($form.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
    exit 2
}

$account = $accountBox.Text.Trim()
if ($account -notmatch '^\d{5,20}$') {
    throw 'QQ号必须是 5-20 位数字。'
}
if ([string]::IsNullOrWhiteSpace($passwordBox.Text)) {
    throw '密码不能为空。'
}

$plainPassword = $passwordBox.Text
$passwordBox.Clear()
$md5 = [System.Security.Cryptography.MD5]::Create()
try {
    $digestBytes = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($plainPassword))
    $digest = -join ($digestBytes | ForEach-Object { $_.ToString('x2') })
    $secureDigest = ConvertTo-SecureString $digest -AsPlainText -Force
    $protectedDigest = ConvertFrom-SecureString $secureDigest
    New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
    Set-Content -LiteralPath $accountFile -Value $account -NoNewline -Encoding ascii
    Set-Content -LiteralPath $credentialFile -Value $protectedDigest -NoNewline -Encoding ascii
}
finally {
    $md5.Dispose()
    $plainPassword = $null
    $digest = $null
}

[System.Windows.Forms.MessageBox]::Show(
    '已配置。下次 NapCat 启动或看门狗恢复时会先复用历史会话，失败后自动尝试密码回退。腾讯要求安全验证时仍需人工确认。',
    '配置完成',
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information
) | Out-Null
