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
".venv\Scripts\pyinstaller.exe" --noconfirm --clean CRFS_IQ_Recorder.spec
if errorlevel 1 goto :error

if defined CRFS_SIGN_THUMBPRINT goto :sign
if defined CRFS_SIGN_PFX goto :sign
echo.
echo Not signed: set CRFS_SIGN_THUMBPRINT or CRFS_SIGN_PFX to sign with a trusted code-signing certificate.
echo Unsigned builds show "Unknown publisher" in SmartScreen. See sign_exe.ps1.
goto :done

:sign
echo Signing exe...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sign_exe.ps1"
if errorlevel 1 goto :error

:done
echo.
echo Done. Give colleagues dist\CRFS_IQ_Recorder.exe - Python is not required.
echo Downloads default to %%USERPROFILE%%\CRFS IQ Recorder\Recordings on the local disk, not OneDrive. Change the folder in Settings.
echo.
if /I not "%CRFS_BUILD_NOPAUSE%"=="1" pause
exit /b 0

:error
echo Build failed.
if /I not "%CRFS_BUILD_NOPAUSE%"=="1" pause
exit /b 1
