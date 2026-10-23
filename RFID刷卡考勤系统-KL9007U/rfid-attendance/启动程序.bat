@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title RFID 学生签到签退系统 (KL9007U)

echo ============================================
echo   RFID 学生签到签退系统   KL9007U
echo ============================================
echo.

rem ----------------------------------------------------------------
rem  自动挑选解释器：优先选"最新的、且带图形界面(tkinter)的" Python
rem  说明：一台电脑常有多个 Python，直接写 python 可能正好是没装齐库的那个
rem ----------------------------------------------------------------
set "PY="
for %%A in ("py -3.13" "py -3.12" "py -3.11" "py -3.10" "python") do (
    if not defined PY (
        %%~A -c "import tkinter" >nul 2>nul
        if not errorlevel 1 set "PY=%%~A"
    )
)

if not defined PY (
    echo [错误] 没有找到可用的 Python ^(需要 3.8 及以上^)。
    echo        请到 https://www.python.org 安装，安装时务必勾选
    echo        "Add Python to PATH"，然后再运行本脚本。
    echo.
    pause
    exit /b 1
)

echo [环境] 本次使用的 Python：
%PY% -c "import sys;print('        ' + sys.executable);print('        版本 ' + sys.version.split()[0])"
echo.

rem ----------------------------------------------------------------
rem  依赖检查：优先用项目内置的 _vendor\serial（无需联网，任何 Python 都能用）
rem ----------------------------------------------------------------
if exist "_vendor\serial\__init__.py" (
    echo [依赖] pyserial：使用项目内置版本 ^(_vendor^)，无需联网。
    goto run
)

%PY% -c "import serial" >nul 2>nul
if not errorlevel 1 (
    echo [依赖] pyserial：使用系统已安装版本。
    goto run
)

echo [依赖] 未找到 pyserial，正在尝试联网安装...
%PY% -m pip install pyserial
echo.

:run
echo 正在启动程序... （关闭程序窗口即可退出）
echo.
%PY% attendance.py
echo.
echo 程序已退出，退出码 %errorlevel%。
pause
