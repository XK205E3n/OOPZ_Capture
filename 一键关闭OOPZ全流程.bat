@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Sending the Feishu shutdown notice, then stopping the gateway...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_oopz_full_stack.ps1" -Notice shutdown
if errorlevel 1 (
  echo ERROR: Some OOPZ Feishu processes could not be stopped.
  pause
  exit /b 1
)
echo OOPZ Feishu gateway has been stopped.
exit /b 0
