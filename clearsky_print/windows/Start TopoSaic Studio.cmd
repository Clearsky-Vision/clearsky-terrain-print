@echo off
setlocal
cd /d "%~dp0"
call "Start Terrain Print.cmd" --check
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" "clearsky_print\studio_boot.py"
if errorlevel 1 pause
