param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
$controller = Join-Path $root '.venv\Scripts\oopz-qq-controller.exe'
$stateRoot = Join-Path $root 'controller_state'
$lockPath = Join-Path $stateRoot 'controller.lock'
if (-not (Test-Path -LiteralPath $controller -PathType Leaf)) {
    throw "QQ controller executable is missing: $controller"
}
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null

function Get-LiveControllerPid {
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { return $null }
    try {
        $record = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
        $processId = [int]$record.pid
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction Stop
        if ($process.CommandLine -and $process.CommandLine.Contains($controller)) { return $processId }
    }
    catch { return $null }
    return $null
}

$livePid = Get-LiveControllerPid
if ($livePid) {
    Write-Output "QQ controller is already running (PID=$livePid)."
    exit 0
}

# A force-stopped controller cannot execute its finally block. Remove only the
# exact lock file after proving that it does not identify a live controller.
if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
    $resolvedLock = (Resolve-Path -LiteralPath $lockPath -ErrorAction Stop).Path
    if ([IO.Path]::GetDirectoryName($resolvedLock) -ne $stateRoot) {
        throw "Unsafe controller lock path: $resolvedLock"
    }
    Remove-Item -LiteralPath $resolvedLock -Force -ErrorAction Stop
}

$quotedCommand = '"' + $controller + '" serve'
Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $quotedCommand -WorkingDirectory $root -WindowStyle Normal

for ($attempt = 1; $attempt -le 100; $attempt++) {
    Start-Sleep -Milliseconds 100
    $livePid = Get-LiveControllerPid
    if ($livePid) {
        Write-Output "QQ controller started (PID=$livePid)."
        exit 0
    }
}
throw 'QQ controller did not acquire its instance lock within 10 seconds.'
