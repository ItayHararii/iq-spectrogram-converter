@echo off
setlocal
cd /d "%~dp0"
title IQ Spectrogram Converter

echo ============================================
echo   IQ Spectrogram Converter
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

if not exist "iq_data.py" (
    echo ERROR: iq_data.py is missing from this folder.
    pause
    exit /b 1
)

if not exist "iq_gui.py" (
    echo ERROR: iq_gui.py is missing from this folder.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo First run: creating virtual environment...
    python -m venv venv
    if errorlevel 1 goto :error
)

echo Checking dependencies...
"venv\Scripts\python.exe" -c "import numpy, matplotlib, seaborn, scipy, soundfile, PIL" >nul 2>&1
if errorlevel 1 (
    echo Installing requirements...
    "venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :error
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
    echo.
)

"venv\Scripts\python.exe" -c "import tkinterdnd2" >nul 2>&1
if errorlevel 1 (
    echo Optional: installing drag-and-drop support...
    "venv\Scripts\python.exe" -m pip install tkinterdnd2 >nul 2>&1
)

if not exist "IQ Results" mkdir "IQ Results"
if not exist "IQ Collection" mkdir "IQ Collection"

if not exist "crfs_iq_recorder\assets\sensorz_icon.ico" if not exist "assets\sensorz_icon.ico" (
    echo WARNING: sensorz_icon.ico missing — taskbar may show the Python icon.
)

echo Refreshing launcher shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
if errorlevel 1 (
    echo WARNING: Could not create shortcut; launching pythonw directly.
    start "" "venv\Scripts\pythonw.exe" "iq_gui.py"
    exit /b 0
)

echo Starting GUI...
start "" "IQ Spectrogram Converter.lnk"
exit /b 0

:error
echo.
echo Setup failed. Review the error shown above.
pause
exit /b 1
