@echo off
setlocal
cd /d "%~dp0"

rem Keep uv and its managed Python inside the project folder
set "TOOLS=%~dp0.tools"
set "UV=%TOOLS%\uv.exe"
set "UV_PYTHON_INSTALL_DIR=%TOOLS%\python"
set "UV_PYTHON_PREFERENCE=only-managed"

if not exist "%UV%" (
    echo [1/4] Installing uv ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%TOOLS%'; $env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex"
    if not exist "%UV%" goto :fail
) else (
    echo [1/4] uv already installed
)

echo [2/4] Creating Python environment ...
if not exist ".venv\Scripts\python.exe" (
    "%UV%" venv --python 3.12 .venv || goto :fail
)

echo [3/4] Installing packages ...
"%UV%" pip install --python .venv\Scripts\python.exe -r requirements.txt || goto :fail

echo [4/4] Downloading default speech model ...
.venv\Scripts\python.exe -m voiceinput.models || goto :fail

echo.
echo Done. Run start.bat to launch VoiceInput.
if "%~1"=="" pause
exit /b 0

:fail
echo.
echo Install failed.
if "%~1"=="" pause
exit /b 1
