@echo off
chcp 65001 >nul
REM ─────────────────────────────────────────────────────────────
REM NapCat 启动模板（相对路径版，可整体搬目录）
REM
REM 与上游 launcher-user.bat 的区别：
REM   1. 全部路径用 %~dp0 推导，不依赖当前工作目录；
REM   2. 优先读环境变量 QQ_EXE，其次才去注册表找 —— 因为
REM      部分受限环境（企业策略 / 安全软件 / 沙箱）会把 reg.exe 拦掉，
REM      而上游脚本硬依赖 reg.exe，那种环境里会直接失败。
REM
REM 用法：
REM   set QQ_EXE=D:\path\to\QQ.exe        （可省略，省略时才查注册表）
REM   launcher.example.bat
REM ─────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"

set "NAPCAT_PATCH_PACKAGE=%~dp0qqnt.json"
set "NAPCAT_LOAD_PATH=%~dp0loadNapCat.js"
set "NAPCAT_INJECT_PATH=%~dp0NapCatWinBootHook.dll"
set "NAPCAT_LAUNCHER_PATH=%~dp0NapCatWinBootMain.exe"
set "NAPCAT_MAIN_PATH=%~dp0napcat.mjs"

if not exist "%NAPCAT_MAIN_PATH%" (
  echo [错误] 没找到 napcat.mjs —— 请先按 README.md 把 NapCat 本体解压到本目录。
  pause
  exit /b 1
)

set "QQPath=%QQ_EXE%"
if not exist "%QQPath%" (
  echo [信息] 环境变量 QQ_EXE 未设置或无效，改从注册表查找 QQ 安装路径……
  for /f "tokens=2*" %%a in ('reg query "HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ" /v "UninstallString"') do set "RetString=%%~b"
  for %%a in ("%RetString%") do set "QQPath=%%~dpaQQ.exe"
)

if not exist "%QQPath%" (
  echo [错误] 找不到 QQ.exe。请设置环境变量 QQ_EXE 指向它，例如：
  echo        set QQ_EXE=D:\Program Files\Tencent\QQNT\QQ.exe
  pause
  exit /b 1
)

echo [信息] QQ       = %QQPath%
echo [信息] NapCat   = %NAPCAT_MAIN_PATH%

REM 生成注入用的加载脚本（内容是本目录的绝对路径，注意正斜杠）
set "NAPCAT_MAIN_POSIX=%NAPCAT_MAIN_PATH:\=/%"
echo (async () =^> {await import("file:///%NAPCAT_MAIN_POSIX%")})() > "%NAPCAT_LOAD_PATH%"

"%NAPCAT_LAUNCHER_PATH%" "%QQPath%" "%NAPCAT_INJECT_PATH%" %*

echo.
echo [信息] 如果 QQ 起来又自己退出，多半是**需要登录机器人号** —— 这个窗口里能看到 QQ 的提示。
pause
