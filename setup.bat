@echo off
setlocal
where python >nul 2>nul
if errorlevel 1 (
    echo Install Python 3.11 through 3.14 from python.org and enable Add Python to PATH.
    pause
    exit /b 1
)
if not exist "%~dp0.venv\Scripts\python.exe" (
    python -m venv "%~dp0.venv"
    if errorlevel 1 goto fail
)
"%~dp0.venv\Scripts\python.exe" -m pip install -e "%~dp0."
if errorlevel 1 goto fail
echo Installation complete. Double-click run.bat to launch Smart File Organizer.
pause
exit /b 0
:fail
echo Installation failed. Check your Python version and internet connection.
pause
exit /b 1
