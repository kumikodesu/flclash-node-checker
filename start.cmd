@echo off
setlocal
if not exist "%~dp0launch.ps1" goto missing
if not exist "%~dp0checker.py" goto missing
if not exist "%~dp0bootstrap.py" goto missing
if not exist "%~dp0vendor\yaml\__init__.py" goto missing
if not exist "%~dp0vendor\requests\__init__.py" goto missing
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
set "taskExit=%errorlevel%"
if not "%taskExit%"=="0" (
    echo.
    echo Startup failed. Error details: "%~dp0startup.log"
    pause
)
exit /b %taskExit%
:missing
echo Please extract the entire ZIP first. Keep all program files and the vendor folder together.
pause
exit /b 1
