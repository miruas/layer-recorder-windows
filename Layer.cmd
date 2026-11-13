@echo off
setlocal
set "LAYER_LAUNCHER="
set "LAYER_ENV_ID="
cd /d "%~dp0"
if exist ".layer-python-env" set /p "LAYER_ENV_ID="<".layer-python-env"
if defined LAYER_ENV_ID set "LAYER_LAUNCHER=%LOCALAPPDATA%\Layer\env\%LAYER_ENV_ID%\Scripts\layer.exe"
if not defined LAYER_LAUNCHER set "LAYER_LAUNCHER=.venv\Scripts\layer.exe"
if not exist "%LAYER_LAUNCHER%" (
    echo Install Layer first with tools\install-windows.ps1 in PowerShell.
    pause
    exit /b 1
)
"%LAYER_LAUNCHER%" %*
exit /b %errorlevel%
