@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Pump-2 backend

rem Keep this file pure ASCII and CRLF - see the note in the launcher bat.
rem cmd.exe desyncs its parser on non-ASCII lines read under a mismatched
rem code page, and the rest of the file runs as garbage commands.

rem Really execute the candidate Python: .venv copied from another machine
rem points at a missing interpreter path, so an existence check is not enough.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -c "pass" >nul 2>&1
if errorlevel 1 set "PY=python"
"%PY%" -c "pass" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] no working Python found - check "python --version"
  pause
  exit /b 1
)

echo ============================================
echo  Pump Room Digital Twin - backend service
echo  window 2 of 2 - start the simulator bat FIRST
echo ============================================
%PY% main.py
pause
