@echo off
chcp 65001 >nul
title Pump Room Digital Twin - Launcher

rem ===========================================================
rem  Pump Room Digital Twin - one click start   (repo root copy)
rem -----------------------------------------------------------
rem  This file must stay PURE ASCII and CRLF. cmd.exe reads a .bat
rem  incrementally and seeks by byte offset, so a single non-ASCII
rem  line desyncs the whole parser - that is why every echoed line
rem  below is English.
rem
rem  The source folder name is Chinese, so it is deliberately NOT
rem  written literally here. It is looked up from the filesystem at
rem  runtime instead (see the FOR loop), which keeps this file
rem  ASCII-only and also survives the folder being renamed.
rem ===========================================================

set "APP_DIR="
if exist "%~dp0backend\sensorSim.py" set "APP_DIR=%~dp0backend"
for /f "delims=" %%D in ('dir /b /ad "%~dp0" 2^>nul') do (
    if exist "%~dp0%%D\backend\sensorSim.py" set "APP_DIR=%~dp0%%D\backend"
)
if not defined APP_DIR goto :noApp
cd /d "%APP_DIR%"

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

:noApp
echo.
echo ============================================================
echo   [ERROR] backend\sensorSim.py not found
echo ============================================================
echo   Tried:
echo       %~dp0backend\sensorSim.py
echo       %~dp0*\backend\sensorSim.py
echo   Keep this file in the project root, next to the source
echo   folder. It does not care what that folder is called.
echo ============================================================
pause
exit /b 1

:noPython
echo.
echo ============================================================
echo   [ERROR] no working Python found
echo ============================================================
echo   A .venv copied between machines only works on the machine
echo   that built it, so this falls back to the system Python.
echo   Install Python 3, or rebuild the virtualenv:
echo       cd /d "%APP_DIR%"
echo       python -m venv .venv
echo       .venv\Scripts\pip install -r requirements.txt
echo ============================================================
pause
exit /b 1
