@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Not installed yet. Run install.bat first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0run.pyw"
