@echo off
chcp 65001 >nul
REM -------------------------------------------------------------
REM NapCat launcher template (relative-path version, directory-safe)
REM
REM PURE ASCII ON PURPOSE. cmd.exe decodes .bat files with the console
REM code page (936/GBK on Chinese Windows). UTF-8 CJK bytes get
REM mis-paired, swallowing the following ASCII character and desyncing
REM the parser -> a wall of "'xxx' is not recognized" errors.
REM Chinese documentation lives in NapCat\README.md and docs\.
REM
REM Differences from the upstream launcher-user.bat:
REM   1. every path is derived from %~dp0, so the current working
REM      directory does not matter;
REM   2. prefers the QQ_EXE environment variable and only falls back to
REM      the registry -- upstream hard-depends on reg.exe, which some
REM      hardened environments (group policy / security suites / sandboxes)
REM      block outright.
REM
REM Usage:
REM   set QQ_EXE=D:\path\to\QQ.exe        (optional; registry is used otherwise)
REM   launcher.example.bat
REM -------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "NAPCAT_PATCH_PACKAGE=%~dp0qqnt.json"
set "NAPCAT_LOAD_PATH=%~dp0loadNapCat.js"
set "NAPCAT_INJECT_PATH=%~dp0NapCatWinBootHook.dll"
set "NAPCAT_LAUNCHER_PATH=%~dp0NapCatWinBootMain.exe"
set "NAPCAT_MAIN_PATH=%~dp0napcat.mjs"

if not exist "%NAPCAT_MAIN_PATH%" (
  echo [ERROR] napcat.mjs not found.
  echo         Unpack the NapCat release into this directory first
  echo         ^(see README.md^).
  pause
  exit /b 1
)

set "QQPath=%QQ_EXE%"
if not exist "%QQPath%" (
  echo [INFO] QQ_EXE is unset or invalid; falling back to the registry...
  for /f "tokens=2*" %%a in ('reg query "HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ" /v "UninstallString"') do set "RetString=%%~b"
  for %%a in ("%RetString%") do set "QQPath=%%~dpaQQ.exe"
)

if not exist "%QQPath%" (
  echo [ERROR] QQ.exe not found. Set QQ_EXE to its full path, e.g.:
  echo         set QQ_EXE=D:\Program Files\Tencent\QQNT\QQ.exe
  pause
  exit /b 1
)

echo [INFO] QQ     = %QQPath%
echo [INFO] NapCat = %NAPCAT_MAIN_PATH%

REM Build the injection loader. The content is this directory's absolute
REM path, with forward slashes on purpose.
set "NAPCAT_MAIN_POSIX=%NAPCAT_MAIN_PATH:\=/%"
echo (async () =^> {await import("file:///%NAPCAT_MAIN_POSIX%")})() > "%NAPCAT_LOAD_PATH%"

"%NAPCAT_LAUNCHER_PATH%" "%QQPath%" "%NAPCAT_INJECT_PATH%" %*

echo.
echo [INFO] If QQ starts and then exits by itself, it is almost always
echo        waiting for you to log in as the BOT account - the QQ window
echo        itself will show the prompt.
pause
