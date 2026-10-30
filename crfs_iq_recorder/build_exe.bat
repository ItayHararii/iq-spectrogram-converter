@echo off
setlocal
cd /d "%~dp0"
title Build CRFS IQ Recorder

rem Outputs:
rem   dist\CRFS_IQ_Recorder.exe          portable one-file EXE (always)
rem   dist\CRFS_IQ_Recorder_Setup.exe    installer for managed PCs (CRFS_BUILD_INSTALLER=1)
rem Signing happens automatically when one of CRFS_SIGN_ARTIFACT_METADATA,
rem CRFS_SIGN_THUMBPRINT or CRFS_SIGN_PFX is set (see sign_exe.ps1 and
rem docs\DISTRIBUTION.md). Bundled DLL/PYD files are signed inside the .spec,
rem the outer EXE and the installer here.

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

set "SIGNING="
if defined CRFS_SIGN_ARTIFACT_METADATA set "SIGNING=1"
if defined CRFS_SIGN_THUMBPRINT set "SIGNING=1"
if defined CRFS_SIGN_PFX set "SIGNING=1"

echo Building one-file exe...
set "CRFS_BUILD_ONEDIR="
".venv\Scripts\pyinstaller.exe" --noconfirm --clean CRFS_IQ_Recorder.spec
if errorlevel 1 goto :error

if "%SIGNING%"=="1" (
    echo Signing exe...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sign_exe.ps1" -Files "%~dp0dist\CRFS_IQ_Recorder.exe"
    if errorlevel 1 goto :error
) else (
    echo.
    echo Not signed: no signing credentials configured. SmartScreen shows "Unknown publisher"
    echo and Smart App Control blocks the EXE until a release is signed. See docs\DISTRIBUTION.md.
)

echo Checking signatures of every executable file in the bundle...
".venv\Scripts\python.exe" packaging\verify_bundle_signatures.py dist\CRFS_IQ_Recorder.exe --report-only

if /I "%CRFS_BUILD_INSTALLER%"=="1" goto :installer
goto :done

:installer
echo.
echo Building folder layout for the installer...
set "CRFS_BUILD_ONEDIR=1"
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --distpath dist\onedir --workpath build\onedir CRFS_IQ_Recorder.spec
set "CRFS_BUILD_ONEDIR="
if errorlevel 1 goto :error
if "%SIGNING%"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sign_exe.ps1" -Files "%~dp0dist\onedir\CRFS_IQ_Recorder\CRFS_IQ_Recorder.exe"
    if errorlevel 1 goto :error
)

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 not found. Install it with:  winget install --id JRSoftware.InnoSetup --exact --scope user
    goto :error
)
for /f "usebackq tokens=*" %%v in (`.venv\Scripts\python.exe -c "import crfs_iq_recorder,sys;sys.stdout.write(crfs_iq_recorder.__version__)"`) do set "APPVER=%%v"
if not defined APPVER (
    echo Could not read __version__ from crfs_iq_recorder\__init__.py
    goto :error
)
echo Compiling installer %APPVER%...
if "%SIGNING%"=="1" (
    "%ISCC%" /Qp /DAppVersion=%APPVER% /DSourceDir=..\dist\onedir\CRFS_IQ_Recorder /DSign "/Scrfs=powershell -NoProfile -ExecutionPolicy Bypass -File $q%~dp0sign_exe.ps1$q -Files $f" packaging\CRFS_IQ_Recorder.iss
) else (
    "%ISCC%" /Qp /DAppVersion=%APPVER% /DSourceDir=..\dist\onedir\CRFS_IQ_Recorder packaging\CRFS_IQ_Recorder.iss
)
if errorlevel 1 goto :error
echo Checking signatures in the installed layout...
".venv\Scripts\python.exe" packaging\verify_bundle_signatures.py dist\onedir\CRFS_IQ_Recorder --report-only
".venv\Scripts\python.exe" packaging\verify_bundle_signatures.py dist\CRFS_IQ_Recorder_Setup.exe --report-only

:done
echo.
echo Done. Portable: dist\CRFS_IQ_Recorder.exe - Python is not required.
if /I "%CRFS_BUILD_INSTALLER%"=="1" echo Installer: dist\CRFS_IQ_Recorder_Setup.exe (silent: /VERYSILENT /SUPPRESSMSGBOXES /NORESTART)
echo Downloads default to %%USERPROFILE%%\CRFS IQ Recorder\Recordings on the local disk, not OneDrive. Change the folder in Settings.
echo.
if /I not "%CRFS_BUILD_NOPAUSE%"=="1" pause
exit /b 0

:error
echo Build failed.
if /I not "%CRFS_BUILD_NOPAUSE%"=="1" pause
exit /b 1
