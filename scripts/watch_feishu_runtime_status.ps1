param(
    [Parameter(Mandatory = $true)]
    [string]$LogPath
)

$Host.UI.RawUI.WindowTitle = 'OOPZ 录音、转写与分析状态'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new() } catch {}
Write-Host '等待录音、转写或分析状态；此窗口只显示运行进度。'
while (-not (Test-Path -LiteralPath $LogPath)) { Start-Sleep -Milliseconds 250 }
Get-Content -LiteralPath $LogPath -Encoding utf8 -Tail 0 -Wait | Where-Object {
    $_ -match '^\[(录制进度|转写进度|分析进度)\]'
}