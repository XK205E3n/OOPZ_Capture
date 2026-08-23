@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT_ROOT=%CD%"
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"

if not exist "%PROJECT_ROOT%\.env" (
  echo ERROR: .env is missing.
  pause
  exit /b 1
)
if not exist "%PYTHON%" (
  echo ERROR: Python runtime is missing: %PYTHON%
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process | Where-Object {$_.Name -ieq 'python.exe' -and $_.CommandLine -and $_.CommandLine -match '(^|\s)-m\s+oopz_capture\.feishu_cli\s+serve(\s|$)'}; if($p){exit 2}else{exit 0}"
if errorlevel 2 (
  echo OOPZ Feishu gateway is already running; no second instance was started.
  exit /b 0
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\start_feishu_windows.ps1" -Lifecycle started
if errorlevel 1 (
  echo ERROR: OOPZ windows could not be started.
  pause
  exit /b 1
)
echo Started two visible windows: Feishu messages and recording/transcription/analysis status.
exit /b 0
