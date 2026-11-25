@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
) else if exist "%~dp0..\..\.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0..\..\.venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

"%PY_EXE%" "%~dp0launcher.py" --port 8897 %*
if errorlevel 1 (
    echo.
    echo LabGuide Studio exited with an error. Press any key to close...
    pause >nul
)
endlocal
