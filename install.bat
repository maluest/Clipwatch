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

if not exist anthropic_key.txt (
    echo.
    echo Claude writes the hook title, post caption and hashtags for each clip.
    echo Paste your Anthropic API key ^(from console.anthropic.com, starts with sk-ant-^)
    echo or just press Enter to skip ^(clips are then made without hooks^):
    set /p ANTHROPIC_KEY_INPUT=API key: 
)
if not exist anthropic_key.txt if not "%ANTHROPIC_KEY_INPUT%"=="" (
    > anthropic_key.txt echo %ANTHROPIC_KEY_INPUT%
    echo Saved in anthropic_key.txt
)

echo.
echo All set! Double-click clip.bat to start clipping a stream.
pause
exit /b

:fail
echo.
echo Something went wrong above. Copy the error and ask for help.
pause
