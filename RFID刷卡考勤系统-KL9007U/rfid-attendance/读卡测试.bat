@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title RFID 读卡测试 (KL9007U)

echo ============================================
echo   RFID 读卡测试 —— 先确认硬件能不能读到卡
echo ============================================
echo.

set "PY="
for %%A in ("py -3.13" "py -3.12" "py -3.11" "py -3.10" "python") do (
    if not defined PY (
        %%~A -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY=%%~A"
    )
)

if not defined PY (
    echo [错误] 没有找到可用的 Python，请先安装 Python 3.8+。
    pause
    exit /b 1
)

echo [环境] 本次使用的 Python：
%PY% -c "import sys;print('        ' + sys.executable);print('        版本 ' + sys.version.split()[0])"
echo.
echo 提示：请先关掉厂商演示软件（UHFReader18CSharp），它会占用串口。
echo       默认使用 COM9 @ 57600；如需换参数，加命令行参数，例如：
echo           python read_test.py COM3 115200
echo.

%PY% read_test.py %*
echo.
pause
