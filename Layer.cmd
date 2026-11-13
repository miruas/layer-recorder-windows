@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\layer.exe" (
    echo Install Layer first with tools\install-windows.ps1 in PowerShell.
    pause
    exit /b 1
)
".venv\Scripts\layer.exe" %*
if errorlevel 1 pause
