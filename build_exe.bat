@echo off
setlocal
cd /d "%~dp0"
title Build CRFS IQ Spectrogram Converter EXE

echo ============================================
echo   Build standalone EXE
echo ============================================
echo.
echo This creates dist\IQ_Spectrogram_Converter.exe
echo First build can take several minutes.
echo.

if not exist "venv\Scripts\python.exe" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python was not found on PATH and venv\ does not exist.
        if /I not "%IQ_BUILD_NOPAUSE%"=="1" pause
        exit /b 1
    )
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 goto :error
)

echo Installing app requirements and PyInstaller...
"venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
"venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo.
echo Building executable (IQ_Spectrogram_Converter.spec)...
"venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean IQ_Spectrogram_Converter.spec
if errorlevel 1 goto :error

if defined CRFS_SIGN_THUMBPRINT goto :sign
if defined CRFS_SIGN_PFX goto :sign
echo.
echo Not signed: set CRFS_SIGN_THUMBPRINT or CRFS_SIGN_PFX to sign with a trusted code-signing certificate.
echo Unsigned builds show "Unknown publisher" in SmartScreen. See sign_exe.ps1 and SECURITY_NOTES.md.
goto :done

:sign
echo Signing exe...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sign_exe.ps1"
if errorlevel 1 goto :error

:done
echo.
echo ============================================
echo   Build complete
echo ============================================
echo.
echo EXE location:
echo   %cd%\dist\IQ_Spectrogram_Converter.exe
echo.
echo Copy that single file anywhere and double-click to run. Python is not required.
echo Output defaults to an "IQ Results" folder next to the EXE.
echo.
if /I not "%IQ_BUILD_NOPAUSE%"=="1" pause
exit /b 0

:error
echo.
echo Build failed. Review the error shown above.
if /I not "%IQ_BUILD_NOPAUSE%"=="1" pause
exit /b 1
