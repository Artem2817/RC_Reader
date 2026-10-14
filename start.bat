@echo off
cd /d "%~dp0"
if exist "dist\RC_Reader.exe" (
    start "" "dist\RC_Reader.exe"
    exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 main.py
    if errorlevel 1 pause
    exit /b
)
python main.py
if errorlevel 1 pause
