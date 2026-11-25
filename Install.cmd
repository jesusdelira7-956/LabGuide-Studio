@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo ========================================================================
echo   LabGuide Studio v3.0.0 - Jesse in the Lab (Windows 11 Installer)
echo ========================================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.11+ was not found on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/ and re-run Install.cmd.
    pause
    exit /b 1
)

if exist "%~dp0..\..\.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0..\..\.venv\Scripts\python.exe"
    echo [OK] Using workspace virtual environment: %PY_EXE%
) else (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
    if not exist "%PY_EXE%" (
        echo [Step 1/4] Creating standalone virtual environment in .venv ...
        python -m venv "%~dp0.venv"
        if errorlevel 1 (
            echo [ERROR] Failed to create .venv.
            pause
            exit /b 1
        )
    )
)

echo [Step 2/4] Installing / verifying Python dependencies...
"%PY_EXE%" -m pip install --upgrade pip >nul 2>nul
"%PY_EXE%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies from requirements.txt.
    pause
    exit /b 1
)

echo [Step 3/4] Installing Desktop shortcut with Jesse in the Lab icon...
"%PY_EXE%" "%~dp0create_desktop_shortcut.py"
if errorlevel 1 (
    echo [WARN] Could not create Desktop shortcut automatically.
)

echo [Step 4/4] Running dependency pre-flight check (Ollama, FFmpeg, Kokoro)...
"%PY_EXE%" "%~dp0launcher.py" --check-only --no-auto-install

echo.
echo [DONE] LabGuide Studio is ready!
echo Launch via Desktop shortcut "LabGuide Studio" or Run_LabGuide_Studio.bat.
echo.
set /p LAUNCH_NOW="Launch LabGuide Studio Web UI now? (Y/N): "
if /I "%LAUNCH_NOW%"=="Y" (
    "%PY_EXE%" "%~dp0launcher.py" --port 8897
)
endlocal
