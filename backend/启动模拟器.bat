@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  泵房数字孪生 - 传感器模拟器（窗口1）
echo  跑起来后别关这个窗口，按 Ctrl+C 退出
echo ============================================
.venv\Scripts\python.exe sensorSim.py
pause
