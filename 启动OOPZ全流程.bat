@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT_ROOT=%CD%"
set "CONTROLLER_EXE=%PROJECT_ROOT%\.venv\Scripts\oopz-qq-controller.exe"
set "GATEWAY_EXE=%PROJECT_ROOT%\.venv\Scripts\oopz-onebot.exe"
set "WATCHDOG_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "WATCHDOG_MODULE=%PROJECT_ROOT%\src\oopz_capture\qq_watchdog.py"
set "NAPCAT_DIR=%PROJECT_ROOT%\NapCatQQ\NapCat.50969.Shell"
set "NAPCAT_START=%NAPCAT_DIR%\start-napcat.bat"
set "LOG_DIR=%PROJECT_ROOT%\logs\launcher"
set "OOPZ_GATEWAY_LIFECYCLE=startup"
set "OOPZ_REUSE_NAPCAT_SESSION=false"
if /I "%~1"=="restarted" set "OOPZ_GATEWAY_LIFECYCLE=restarted"
if /I "%~2"=="reuse-session" set "OOPZ_REUSE_NAPCAT_SESSION=true"

echo.
echo ===== OOPZ full-stack launcher =====
echo Project: %PROJECT_ROOT%
echo.

if not exist "%PROJECT_ROOT%\.env" (
  echo ERROR: .env is missing.
  goto :failed
)
if not exist "%CONTROLLER_EXE%" (
  echo ERROR: QQ controller executable is missing.
  goto :failed
)
if not exist "%GATEWAY_EXE%" (
  echo ERROR: OneBot gateway executable is missing.
  goto :failed
)
if not exist "%WATCHDOG_PYTHON%" (
  echo ERROR: QQ watchdog Python runtime is missing.
  goto :failed
)
if not exist "%WATCHDOG_MODULE%" (
  echo ERROR: QQ watchdog module is missing.
  goto :failed
)
if not exist "%NAPCAT_START%" (
  echo ERROR: NapCat start script is missing.
  goto :failed
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo [1/5] Checking OneBot configuration...
"%GATEWAY_EXE%" validate-config > "%LOG_DIR%\onebot-preflight.log" 2>&1
if errorlevel 1 (
  echo ERROR: OneBot configuration is invalid. See onebot-preflight.log.
  goto :failed
)

echo [2/5] Starting QQ controller...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\ensure_qq_controller.ps1" -ProjectRoot "%PROJECT_ROOT%" > "%LOG_DIR%\controller-start.log" 2>&1
if errorlevel 1 (
  echo ERROR: QQ controller could not be started. See controller-start.log.
  goto :failed
)

echo [3/5] Starting NapCat QQ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -State Listen -LocalPort 3001 -ErrorAction SilentlyContinue) { exit 0 }; exit 1" >nul 2>&1
if not errorlevel 1 goto :napcat_ready

if exist "%PROJECT_ROOT%\controller_state\napcat_password_md5.dpapi" (
  echo Encrypted NapCat fallback credential found; starting without a password prompt...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\start_napcat_saved_session.ps1" -NapCatDirectory "%NAPCAT_DIR%"
) else if /I "%OOPZ_REUSE_NAPCAT_SESSION%"=="true" (
  echo Reusing the saved QQ login session for NapCat...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\start_napcat_saved_session.ps1" -NapCatDirectory "%NAPCAT_DIR%"
) else (
  echo Opening the QQ account and password dialog...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\start_napcat_password.ps1" -NapCatDirectory "%NAPCAT_DIR%"
)
if errorlevel 1 (
  echo ERROR: NapCat login was cancelled or could not be started.
  goto :failed
)
echo Waiting for NapCat. Complete any QQ verification window if it appears...
for /l %%I in (1,1,60) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -State Listen -LocalPort 3001 -ErrorAction SilentlyContinue) { exit 0 }; exit 1" >nul 2>&1
  if not errorlevel 1 goto :napcat_ready
  ping 127.0.0.1 -n 2 >nul
)
echo ERROR: NapCat did not open OneBot port 3001 within 60 seconds.
goto :failed

:napcat_ready
echo [4/5] Restarting OneBot gateway and notifying administrators...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\restart_onebot_gateway.ps1" -Lifecycle "%OOPZ_GATEWAY_LIFECYCLE%" > "%LOG_DIR%\onebot-restart.log" 2>&1
if errorlevel 1 (
  echo ERROR: OneBot gateway could not be restarted. See onebot-restart.log.
  goto :failed
)

echo [5/5] Starting isolated QQ recovery watchdog...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'python.exe' -and $_.CommandLine -and $_.CommandLine.Contains('oopz_capture.qq_watchdog')}; if(-not $p){Start-Process -FilePath '%WATCHDOG_PYTHON%' -ArgumentList '-m oopz_capture.qq_watchdog serve' -WorkingDirectory '%PROJECT_ROOT%' -WindowStyle Hidden}" > "%LOG_DIR%\qq-watchdog-start.log" 2>&1
if errorlevel 1 (
  echo ERROR: QQ recovery watchdog could not be started. See qq-watchdog-start.log.
  goto :failed
)

echo.
echo All components were started. QQ recovery watchdog is running in the background.
echo Logs: %LOG_DIR%
ping 127.0.0.1 -n 4 >nul
exit /b 0

:failed
echo.
echo Startup did not complete. Press any key to close this window.
pause >nul
exit /b 1
