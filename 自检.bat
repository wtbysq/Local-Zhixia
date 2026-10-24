@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
title 本地模型工作台 · 自检

where python >nul 2>nul
if errorlevel 1 goto NOPY

python -u scripts\doctor.py
goto END

:NOPY
echo [X] 未找到 python 命令。
echo     请安装 Python 3.9 以上，安装时勾选 "Add python.exe to PATH"。

:END
echo.
echo 按任意键关闭。
pause >nul
