@echo off
REM Double-click to watch a stream. Extra options can be added after clip.bat when run from a terminal,
REM e.g.  clip.bat --sensitivity 2.5 --layout facecam --cam 0.75,0.05,0.22,0.25
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Run install.bat first.
    pause
    exit /b
)
REM Keep the stream downloader current - YouTube/Twitch/Kick change things often
.venv\Scripts\python -m pip install --quiet --upgrade "yt-dlp[default]" streamlink >nul 2>nul
set /p URL=Paste the LIVE stream link (Twitch, YouTube or Kick) and press Enter:
.venv\Scripts\python -m clipwatch watch "%URL%" %*
echo.
echo Your clips are in the "clips" folder.
pause
