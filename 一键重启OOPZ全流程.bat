@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo ===== OOPZ full-stack restart =====
if not exist "%~dp0controller_state\logs" mkdir "%~dp0controller_state\logs"
echo Checking whether NapCat and the QQ account are healthy...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\test_napcat_health.ps1" > "%~dp0controller_state\logs\napcat-restart-health.log" 2>&1
if errorlevel 1 (
  set "OOPZ_PRESERVE_NAPCAT=false"
  echo NapCat is unavailable or QQ is offline; NapCat will be recovered during this restart.
) else (
  set "OOPZ_PRESERVE_NAPCAT=true"
  echo NapCat and QQ are healthy; preserving the existing login process.
)

echo Stopping current OOPZ components...
if /I "%OOPZ_PRESERVE_NAPCAT%"=="true" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_oopz_full_stack.ps1" -Notice restarting -PreserveNapCat
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_oopz_full_stack.ps1" -Notice restarting
)
set "OOPZ_STOP_RESULT=%ERRORLEVEL%"
if not "%OOPZ_STOP_RESULT%"=="0" (
  echo ERROR: Some OOPZ processes could not be stopped.
  pause
  exit /b 1
)

echo OOPZ components stopped successfully.
echo Starting OOPZ again...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\invoke_full_stack_launcher.ps1" -Restart
set "OOPZ_START_RESULT=%ERRORLEVEL%"
exit /b %OOPZ_START_RESULT%
