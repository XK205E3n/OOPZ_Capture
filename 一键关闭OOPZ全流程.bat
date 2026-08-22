@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo ===== OOPZ full-stack shutdown =====
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_oopz_full_stack.ps1"
if errorlevel 1 (
  echo ERROR: Some OOPZ processes could not be stopped.
  pause
  exit /b 1
)

echo OOPZ NapCat, controller, and OneBot gateway have been stopped.
echo Only OOPZ project components were managed by this command.
ping 127.0.0.1 -n 3 >nul
exit /b 0
