@echo off
chcp 65001 >nul
REM =============================================================
REM  XiaoXingJiu installer - Windows double-click entry point
REM
REM  WHY THIS FILE IS PURE ASCII (never add CJK text here):
REM    cmd.exe decodes a .bat with the console code page (936/GBK on
REM    a Chinese Windows). UTF-8 CJK bytes get mis-paired, which
REM    swallows the next ASCII character and desyncs the parser -
REM    the result is a wall of "'xxx' is not recognized as an
REM    internal or external command" errors, and even "install.ps1"
REM    degrades into "nstall.ps1".
REM    Measured: any CJK before "chcp 65001" always breaks the file;
REM    and even with chcp on line 2 it still breaks when the .bat is
REM    CALLed from another .bat. Pure ASCII has no such failure mode.
REM    All Chinese output lives in install.ps1 (UTF-8 with BOM).
REM    Run "python tools/normalize_scripts.py" to check.
REM
REM  WHY A .bat AT ALL:
REM    The default PowerShell execution policy blocks .ps1, so
REM    double-clicking install.ps1 just flashes a window. This
REM    launches it with -ExecutionPolicy Bypass and pauses at the
REM    end so a double-click user can actually read the result.
REM
REM  LOGIC IS NOT DUPLICATED HERE - it all lives in install.ps1.
REM
REM  Non-interactive use (CI / scripting): set QQBOT_NO_PAUSE=1
REM  to skip the final keypress wait.
REM =============================================================
setlocal
cd /d "%~dp0"

echo.
echo   XiaoXingJiu  -  one-click installer (Windows)
echo   ---------------------------------------------
echo.
echo   Just double-click it, or pass flags through to install.ps1:
echo.
echo     -DetectOnly [-Json]   probe this machine only, change nothing
echo     -CheckOnly            dry run (CI), change nothing
echo     -Yes                  unattended: install system deps without asking
echo     -NoSystem             never touch the system package manager
echo     -DryRun               print the plan, do nothing
echo     -Mirror ^<mode^>        auto ^(default^) / off / tuna / aliyun
echo     -WithBrowser          also install Playwright chromium ^(~150 MB^)
echo     -AllowDownload        allow downloading the python.org installer
echo     -TargetDir ^<path^>     -RepoUrl ^<url^>
echo.
echo   Example:  install.bat -DetectOnly
echo.

where powershell >nul 2>&1
if errorlevel 1 (
  echo   [xx] powershell not found.
  echo        Install Python 3.11+ ^(tick "Add to PATH"^) and read the
  echo        deployment guide under docs\ before retrying.
  if not defined QQBOT_NO_PAUSE (
    echo.
    echo   Press any key to close this window . . .
    pause >nul
  )
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo   Done - all health checks passed.
) else if "%RC%"=="1" (
  echo   Done, but some checks raised warnings. Scroll up for the [!!] lines.
) else (
  echo   Finished with errors. Scroll up for the [xx] lines.
)

if not defined QQBOT_NO_PAUSE (
  echo.
  echo   Press any key to close this window . . .
  pause >nul
)
exit /b %RC%
