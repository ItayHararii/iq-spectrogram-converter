@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run "Start CRFS IQ Recorder.bat" once to create the environment.
    pause
    exit /b 1
)
set PYTHONPATH=%~dp0
start "" ".venv\Scripts\pythonw.exe" -m crfs_iq_recorder %*
