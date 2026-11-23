@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call :create_environment
    if errorlevel 1 exit /b 1
)
if not exist ".venv\.terrain-print-ready" (
    echo Installing imagery libraries on this first run...
    ".venv\Scripts\python.exe" -m pip --disable-pip-version-check install --only-binary=:all: -r "clearsky_print\requirements.txt"
    if errorlevel 1 goto failed
    ".venv\Scripts\python.exe" -c "import tkinter, numpy, PIL, rasterio"
    if errorlevel 1 goto failed
    echo ready>".venv\.terrain-print-ready"
)
if /I "%~1"=="--check" exit /b 0
start "" ".venv\Scripts\pythonw.exe" "clearsky_print\desktop.py"
exit /b 0

:create_environment
set "terrain_python=-3.13"
py %terrain_python% -c "import struct, tkinter; assert struct.calcsize('P') == 8" 2>nul
if not errorlevel 1 goto install_environment
set "terrain_python=-3"
py %terrain_python% -c "import struct, sys, tkinter; assert sys.version_info >= (3, 11) and struct.calcsize('P') == 8"
if errorlevel 1 goto python_required

:install_environment
echo Creating a local Python environment for Terrain Print...
py %terrain_python% -m venv ".venv"
if errorlevel 1 goto failed
exit /b 0

:python_required
echo Install 64-bit Python 3.13 with Tcl/Tk and the Windows py launcher.
echo https://www.python.org/downloads/windows/
pause
exit /b 1

:failed
echo Terrain Print setup could not finish. Review the messages above and try again.
echo If pip could not find a compatible wheel, install 64-bit Python 3.13.
pause
exit /b 1
