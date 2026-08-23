@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT_ROOT=%CD%"
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo ERROR: Python runtime is missing: %PYTHON%
  pause
  exit /b 1
)

echo Sending the Feishu restart notice, then stopping the current gateway...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_oopz_full_stack.ps1" -Notice restarting
if errorlevel 1 (
  echo ERROR: The current OOPZ Feishu gateway could not be stopped.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\start_feishu_windows.ps1" -Lifecycle restarted
if errorlevel 1 (
  echo ERROR: OOPZ windows could not be started.
  pause
  exit /b 1
)
echo Started two visible windows. The group receives restart completion after reconnecting; help is not sent on restart.
exit /b 0
