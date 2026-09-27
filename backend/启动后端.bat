@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  泵房数字孪生 - 后端服务（窗口2）
echo  前提：另开一个窗口先跑 启动模拟器.bat
echo ============================================
.venv\Scripts\python.exe main.py
pause
