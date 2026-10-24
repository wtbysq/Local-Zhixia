@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
title 本地智匣

where python >nul 2>nul
if errorlevel 1 goto NOPY

echo ============================================================
echo   本地智匣
echo ============================================================
echo   引擎按需启动：第一次对话时自动加载模型，可能需要几十秒。
echo   完全离线运行，数据不出本机。
echo ============================================================
echo.

start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8787/"
python -u server\main.py
echo.
echo 服务已退出。若上面有报错，运行「自检.bat」定位。
goto END

:NOPY
echo [X] 未找到 python 命令。
echo     请安装 Python 3.9 以上，安装时勾选 "Add python.exe to PATH"。

:END
echo.
echo 按任意键关闭。
pause >nul
