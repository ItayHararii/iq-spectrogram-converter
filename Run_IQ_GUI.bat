@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo Virtual environment not found.
    echo Double-click "Start IQ Converter.bat" first — it will set everything up.
    pause
    exit /b 1
)

start "" "venv\Scripts\pythonw.exe" "iq_gui.py"
