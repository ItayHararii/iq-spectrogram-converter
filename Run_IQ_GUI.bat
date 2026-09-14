@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo Virtual environment not found.
    echo Double-click "Start IQ Converter.bat" first — it will set everything up.
    pause
    exit /b 1
)

if not exist "crfs_iq_recorder\assets\sensorz_icon.ico" if not exist "assets\sensorz_icon.ico" (
    echo WARNING: sensorz_icon.ico missing — taskbar may show the Python icon.
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" >nul 2>&1
if exist "IQ Spectrogram Converter.lnk" (
    start "" "IQ Spectrogram Converter.lnk"
) else (
    start "" "venv\Scripts\pythonw.exe" "iq_gui.py"
)
