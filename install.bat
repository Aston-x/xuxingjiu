@echo off
REM ─────────────────────────────────────────────────────────────
REM 许杏玖 · 一键安装（Windows 双击入口）
REM
REM 为什么要有这个 .bat：Windows 默认的执行策略会把 .ps1 直接拦下，
REM 双击 .ps1 多半只看到一闪而过的窗口。这里用 Bypass 起 PowerShell，
REM 并在最后 pause，让双击的人能看到结果。
REM
REM 逻辑**不在这里**重复实现 —— 全部在 install.ps1 里，避免两份代码走偏。
REM ─────────────────────────────────────────────────────────────
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   许杏玖 · 一键安装
echo   ---------------------------------------------
echo.

where powershell >nul 2>&1
if errorlevel 1 (
  echo   [xx] 找不到 powershell。请手动装 Python 3.11+ 后按 docs\部署-Windows.md 操作。
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  echo   安装完成，体检全绿。
) else if "%RC%"=="1" (
  echo   安装完成，但体检有几项警告，往上翻看 [!!] 那几行。
) else (
  echo   安装过程中有错误，往上翻看 [xx] 那几行。
)
echo.
pause
exit /b %RC%
