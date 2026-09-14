@echo off
setlocal
cd /d "%~dp0"
title CRFS IQ Recorder

echo ============================================
echo   CRFS IQ Recorder
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo Install Python 3 from https://www.python.org/downloads/
    echo Make sure "Add python.exe to PATH" is checked.
    echo.
    pause
    exit /b 1
)

if not exist "crfs_iq_recorder\__main__.py" (
    echo ERROR: Application package is missing from this folder.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :error
)

echo Checking dependencies...
".venv\Scripts\python.exe" -c "import PySide6, requests, paramiko, numpy, matplotlib" >nul 2>&1
if errorlevel 1 (
    echo Installing requirements...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
    echo.
)

set PYTHONPATH=%~dp0
echo Starting GUI...
start "" ".venv\Scripts\pythonw.exe" -m crfs_iq_recorder %*
exit /b 0

:error
echo.
echo Setup failed. Review the error shown above.
pause
exit /b 1
