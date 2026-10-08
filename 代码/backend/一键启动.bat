@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Pump Room Digital Twin - Launcher

rem This file must stay pure ASCII and CRLF: cmd.exe parses .bat by byte
rem offset, and one non-ASCII line desyncs the whole parser.

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -c "pass" >nul 2>&1
if errorlevel 1 set "PY=python"
"%PY%" -c "pass" >nul 2>&1
if errorlevel 1 goto :noPython

echo ============================================================
echo   Pump Room Digital Twin - one click start
echo ============================================================
echo   Python : %PY%
echo   Folder : %CD%
echo.

netstat -ano | findstr /r /c:":502 .*LISTENING" >nul 2>&1
if not errorlevel 1 echo   [warn] port 502 busy - close old python first
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul 2>&1
if not errorlevel 1 echo   [warn] port 8000 busy - close old python first
echo.

echo   [1/3] starting sensor simulator (Modbus 127.0.0.1:502) ...
start "Pump-1 simulator" cmd /k %PY% sensorSim.py

echo         waiting 3s so the simulator binds the port first ...
timeout /t 3 /nobreak >nul

echo   [2/3] starting backend (http://127.0.0.1:8000) ...
start "Pump-2 backend" cmd /k %PY% main.py

echo         waiting 7s for the backend to come up ...
timeout /t 7 /nobreak >nul

echo   [3/3] opening browser ...
start "" "http://127.0.0.1:8000/"

echo.
echo ============================================================
echo   Keep the two new windows OPEN - closing them stops it.
echo ============================================================
pause
exit /b 0

:noPython
echo.
echo ============================================================
echo   [ERROR] no working Python found
echo ============================================================
echo   Rebuild the virtualenv in this folder:
echo       python -m venv .venv
echo       .venv\Scripts\pip install -r requirements.txt
echo ============================================================
pause
exit /b 1
