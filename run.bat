@echo off
setlocal
rem ---------------------------------------------------------------
rem  Launcher for main.py (Art-Net to JSON Sequence Recorder)
rem  Usage:  run.bat          - start the app (no console window)
rem          run.bat debug    - start with a console to see errors
rem  Keep this file in the same folder as main.py
rem ---------------------------------------------------------------
cd /d "%~dp0"
set "SCRIPT=main.py"

if not exist "%SCRIPT%" (
    echo Cannot find %SCRIPT% in "%CD%"
    pause
    exit /b 1
)

if /i "%~1"=="debug" goto debug

rem Normal start: windowless Python (the Python launcher first, then pythonw)
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%SCRIPT%"
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%SCRIPT%"
    exit /b 0
)
goto nopython

:debug
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT%"
    goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT%"
    goto finished
)
goto nopython

:finished
echo.
echo The application has exited (exit code %errorlevel%).
pause
exit /b 0

:nopython
echo Python 3 was not found.
echo Install Python 3.9 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup, then run this file again.
pause
exit /b 1
