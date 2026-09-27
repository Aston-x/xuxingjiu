@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QZone Bridge

REM PURE ASCII ON PURPOSE -- see the note in install.bat.
REM cmd.exe decodes .bat files with the console code page (936/GBK on
REM Chinese Windows); UTF-8 CJK bytes desync the parser and every
REM following line turns into "'xxx' is not recognized".
REM Chinese docs: docs\ and the web console at http://127.0.0.1:6200/

if not exist "test_cache" mkdir test_cache

rem By default the output goes to qzone-bridge.log -- that is exactly the
rem file the console's "QZone bridge" log page reads.
rem Want it on screen instead: start.bat visible
set LOGFILE=%~dp0qzone-bridge.log

if /i "%~1"=="visible" goto :visible

echo QZone Bridge is starting in the background. Output goes to:
echo   %LOGFILE%
echo.
echo (or open the log page in the console at http://127.0.0.1:6200/)
echo A login QR code, if needed, is saved to test_cache\qrcode.png
echo.
echo >> "%LOGFILE%" echo.
echo >> "%LOGFILE%" echo ===== %DATE% %TIME% start =====
call :run >> "%LOGFILE%" 2>&1
echo Bridge exited. See the log for details.
pause
goto :eof

:visible
echo QZone Bridge (visible mode, output NOT written to a file)
echo A login QR code, if needed, is saved to test_cache\qrcode.png
echo.
call :run
echo.
echo Bridge stopped.
pause
goto :eof

:run
rem node lookup order: NODE_EXE environment variable, then "node" on PATH.
rem (No hard-coded absolute path -- moving drives or machines needs no edit.)
set "NODE_BIN=%NODE_EXE%"
if not defined NODE_BIN set "NODE_BIN=node"
"%NODE_BIN%" node_modules\tsx\dist\cli.mjs src\main.ts
goto :eof
