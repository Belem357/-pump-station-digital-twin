@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Pump Room Digital Twin - Launcher

rem ============================================================
rem  IMPORTANT: keep this file pure ASCII.
rem  cmd.exe parses .bat by byte offset; a non-ASCII line read under a
rem  mismatched code page desyncs the parser and the rest of the file
rem  gets executed as garbage commands. chcp 65001 below keeps PYTHON
rem  output (Chinese + emoji) correct, but the .bat itself must stay ASCII.
rem  Also keep CRLF line endings.
rem ============================================================

rem Pick a Python that actually runs. Do NOT just test whether .venv exists:
rem a virtualenv copied from another machine points at an interpreter path
rem that does not exist here, so we must really execute it once.
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

rem Ports may still be held by a previous run - warn before confusing errors
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
echo.
echo   This machine : http://127.0.0.1:8000/
echo   LAN / phone  : read the LAN address printed in the
echo                  "Pump-2 backend" window and open it on a
echo                  phone connected to the SAME WiFi.
echo.
echo   Phone cannot connect but this machine can - Windows
echo   Firewall is blocking inbound port 8000. Allow it.
echo ============================================================
pause
exit /b 0

:noPython
echo.
echo ============================================================
echo   [ERROR] no working Python found
echo ============================================================
echo   Check that "python --version" works in a command prompt.
echo.
echo   Or rebuild the virtualenv in this folder (backend):
echo       python -m venv .venv
echo       .venv\Scripts\pip install -r requirements.txt
echo.
echo   Never copy .venv from another machine - it hardcodes the
echo   interpreter path and will not work here. That is why
echo   .gitignore excludes it.
echo ============================================================
pause
exit /b 1
