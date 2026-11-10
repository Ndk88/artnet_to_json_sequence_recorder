@echo off
setlocal
rem ---------------------------------------------------------------
rem  Launcher for packets_tester.py (test Art-Net source with a log)
rem  The console stays open so you can read the sending log and errors.
rem
rem  Usage:    run_packets_tester.bat [host] [port] [chips] [universe]
rem  Example:  run_packets_tester.bat 127.0.0.1 6454 104 1
rem  Defaults: 127.0.0.1  6454  104  1
rem  Stop with Ctrl+C. Keep this file in the same folder as packets_tester.py
rem ---------------------------------------------------------------
cd /d "%~dp0"
title Art-Net packets tester
set "SCRIPT=packets_tester.py"

if not exist "%SCRIPT%" (
    echo Cannot find %SCRIPT% in "%CD%"
    pause
    exit /b 1
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -u "%SCRIPT%" %*
    goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
    python -u "%SCRIPT%" %*
    goto finished
)
goto nopython

:finished
set "CODE=%errorlevel%"
echo.
echo The tester has exited (exit code %CODE%).
pause
exit /b %CODE%

:nopython
echo Python 3 was not found.
echo Install Python 3.9 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup, then run this file again.
pause
exit /b 1
