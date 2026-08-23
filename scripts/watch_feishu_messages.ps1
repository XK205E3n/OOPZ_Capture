param(
    [Parameter(Mandatory = $true)]
    [string]$LogPath,
    [Parameter(Mandatory = $true)]
    [string]$ErrorLogPath
)

$Host.UI.RawUI.WindowTitle = 'OOPZ 飞书消息收发记录（实时）'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new() } catch {}
Write-Host "正在实时监听飞书消息、卡片和文件发送记录。日志：$LogPath"

# Do not use Get-Content -Wait here. The runtime log has more than one writer
# during a capture, and its long-lived follow stream can stop yielding after a
# concurrent append. Reopen the UTF-8 file on a short interval instead.
$seenLineCount = 0
$firstRead = $true
function Format-FeishuLogLine([string]$Line) {
    if ($Line -notmatch '^\[飞书识别\] ') { return $Line }
    $command = $Line.Substring('[飞书识别] '.Length)
    $labels = @{
        '/oopz 帮助' = '帮助'; '/oopz 状态' = '状态'; '/oopz 离开' = '停止'
        '/oopz 最近报告' = '最近报告'; '/oopz 详细报告' = '详细报告'
        '/oopz 待分析' = '待分析'; '/oopz 删除会话' = '删除会话'
        '/oopz 设置状态' = '设置状态'
    }
    if ($labels.ContainsKey($command)) { return "[飞书识别] $($labels[$command])" }
    if ($command.StartsWith('/oopz 开始')) { return '[飞书识别] 开始录音' + $command.Substring('/oopz 开始'.Length) }
    if ($command.StartsWith('/oopz 设置')) { return '[飞书识别] 设置' + $command.Substring('/oopz 设置'.Length) }
    if ($command.StartsWith('/oopz 删除会话 ')) { return '[飞书识别] 删除会话 ' + $command.Substring('/oopz 删除会话 '.Length) }
    return $Line
}
while ($true) {
    if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
        Start-Sleep -Milliseconds 500
        continue
    }
    try {
        $lines = @(Get-Content -LiteralPath $LogPath -Encoding utf8 -ErrorAction Stop)
    } catch {
        Start-Sleep -Milliseconds 500
        continue
    }
    if ($lines.Count -lt $seenLineCount) {
        # Recover if an operator replaces or manually truncates the log.
        $seenLineCount = 0
    }
    if ($firstRead) {
        $seenLineCount = [Math]::Max(0, $lines.Count - 40)
        $firstRead = $false
    }
    for ($index = $seenLineCount; $index -lt $lines.Count; $index++) {
        $line = $lines[$index]
        if ($line -match '^\[飞书(收信|识别|发信|发卡|发件)\]') {
            Write-Output (Format-FeishuLogLine $line)
        }
    }
    $seenLineCount = $lines.Count
    Start-Sleep -Milliseconds 500
}
