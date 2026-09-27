@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QZone Bridge

if not exist "test_cache" mkdir test_cache

rem 默认把输出写进 qzone-bridge.log —— 控制面板的「空间桥接」日志页读的就是它。
rem 想直接在窗口看输出：start.bat visible
set LOGFILE=%~dp0qzone-bridge.log

if /i "%~1"=="visible" goto :visible

echo QZone Bridge 正在后台启动，输出写入：
echo   %LOGFILE%
echo.
echo （控制面板 http://127.0.0.1:6200/ 的「日志」页可直接看）
echo 需要扫码时二维码保存在 test_cache\qrcode.png
echo.
echo >> "%LOGFILE%" echo.
echo >> "%LOGFILE%" echo ===== %DATE% %TIME% 启动 =====
call :run >> "%LOGFILE%" 2>&1
echo 桥接已退出，详见日志。
pause
goto :eof

:visible
echo QZone Bridge（可见模式，输出不落文件）
echo 需要扫码时二维码保存在 test_cache\qrcode.png
echo.
call :run
echo.
echo Bridge stopped.
pause
goto :eof

:run
rem node 的查找顺序：环境变量 NODE_EXE > PATH 里的 node
rem （不写死任何本机绝对路径 —— 换机器、换盘符都不用改这个脚本）
set "NODE_BIN=%NODE_EXE%"
if not defined NODE_BIN set "NODE_BIN=node"
"%NODE_BIN%" node_modules\tsx\dist\cli.mjs src\main.ts
goto :eof
