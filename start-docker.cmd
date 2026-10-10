@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0docker-launch.ps1" %*
if errorlevel 1 (
  echo Docker startup failed. See docker-startup.log in this folder.
  pause
  exit /b 1
)
