@echo off
setlocal
echo Stopping bot terminal...
taskkill /FI "WINDOWTITLE eq BOT_SERVER_BUILDER*" /T /F >nul 2>&1
if %errorlevel%==0 (
  echo Bot stopped successfully.
) else (
  echo No running bot terminal found.
)
timeout /t 2 >nul
