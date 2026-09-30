@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo First run: double-click setup.bat to install the application.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py" %*
if errorlevel 1 pause
