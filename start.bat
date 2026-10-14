@echo off
cd /d "%~dp0"
if exist "dist\RC_Reader.exe" (
    start "" "dist\RC_Reader.exe"
    exit /b 0
)
py main.py
if errorlevel 1 pause
