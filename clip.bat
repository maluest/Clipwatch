@echo off
REM Double-click to watch a stream. Extra options can be added after clip.bat when run from a terminal,
REM e.g.  clip.bat --sensitivity 2.5 --layout facecam --cam 0.75,0.05,0.22,0.25
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Run install.bat first.
    pause
    exit /b
)
set /p URL=Paste the live stream link (Twitch, YouTube or Kick) and press Enter:
.venv\Scripts\python -m clipwatch watch "%URL%" %*
echo.
echo Your clips are in the "clips" folder.
pause
