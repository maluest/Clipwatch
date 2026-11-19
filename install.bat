@echo off
REM One-time setup for clipwatch on Windows. Double-click this file.
REM Safe to run again any time (e.g. after downloading a newer version).
cd /d "%~dp0"

set NEED_RESTART=0
where py >nul 2>nul
if errorlevel 1 (
    echo Installing Python...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    set NEED_RESTART=1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo Installing FFmpeg...
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    set NEED_RESTART=1
)
where deno >nul 2>nul
if errorlevel 1 (
    echo Installing Deno (needed for YouTube^)...
    winget install -e --id DenoLand.Deno --accept-package-agreements --accept-source-agreements
    set NEED_RESTART=1
)
if "%NEED_RESTART%"=="1" (
    echo.
    echo Some programs were just installed. Close this window and double-click install.bat again.
    pause
    exit /b
)

if not exist .venv (
    echo Creating a private Python environment...
    py -3 -m venv .venv || goto :fail
)
echo Installing clipwatch and its parts (a few minutes the first time)...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install --upgrade -e . || goto :fail

echo.
where claude >nul 2>nul
if errorlevel 1 (
    echo Claude writes each clip's hook title, caption and hashtags using YOUR Claude subscription,
    echo through Claude Code. It isn't installed yet. To set it up:
    echo   1. Open PowerShell ^(normal, not as administrator^) and run:
    echo        irm https://claude.ai/install.ps1 ^| iex
    echo   2. Close and reopen PowerShell, run:  claude   and log in with your Claude account.
    echo   3. Run install.bat again.
    echo Until then, clips are made without hooks.
) else (
    echo Checking Claude Code is logged in...
    .venv\Scripts\python -m clipwatch check-claude
)

echo.
echo All set! Double-click clip.bat to start clipping a stream.
pause
exit /b

:fail
echo.
echo Something went wrong above. Copy the error and ask for help.
pause
