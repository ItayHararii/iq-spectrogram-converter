@echo off
setlocal
cd /d "%~dp0"
title Build CRFS IQ Recorder

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :error
)

echo Installing application and PyInstaller...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo Building exe...
".venv\Scripts\pyinstaller.exe" --noconfirm CRFS_IQ_Recorder.spec
if errorlevel 1 goto :error

echo.
echo Done. Give colleagues dist\CRFS_IQ_Recorder.exe — Python is not required.
echo Downloads default to %USERPROFILE%\CRFS IQ Recorder\Recordings (local disk, not OneDrive)
echo.
if /I not "%CRFS_BUILD_NOPAUSE%"=="1" pause
exit /b 0

:error
echo Build failed.
if /I not "%CRFS_BUILD_NOPAUSE%"=="1" pause
exit /b 1
