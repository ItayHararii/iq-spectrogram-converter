@echo off
setlocal
cd /d "%~dp0"
title Build IQ Spectrogram Converter EXE

echo ============================================
echo   Build standalone EXE
echo ============================================
echo.
echo This creates dist\IQ_Spectrogram_Converter.exe
echo First build can take several minutes.
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 goto :error
)

echo Installing app requirements...
"venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo Installing PyInstaller...
"venv\Scripts\python.exe" -m pip install pyinstaller
if errorlevel 1 goto :error

echo.
echo Building executable...
"venv\Scripts\pyinstaller.exe" ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --onefile ^
    --name "IQ_Spectrogram_Converter" ^
    --hidden-import=iq_data ^
    --hidden-import=numpy ^
    --hidden-import=matplotlib ^
    --hidden-import=seaborn ^
    --hidden-import=scipy ^
    --hidden-import=soundfile ^
    --collect-all matplotlib ^
    --collect-all seaborn ^
    iq_gui.py

if errorlevel 1 goto :error

echo.
echo ============================================
echo   Build complete
echo ============================================
echo.
echo EXE location:
echo   %cd%\dist\IQ_Spectrogram_Converter.exe
echo.
echo You can copy that single file anywhere and double-click to run.
echo Keep WAV files handy; output defaults to an "IQ Results" folder next to the EXE.
echo.
pause
exit /b 0

:error
echo.
echo Build failed. Review the error shown above.
pause
exit /b 1
