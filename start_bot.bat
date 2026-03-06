@echo off
setlocal
cd /d "%~dp0"
title BOT_SERVER_BUILDER
echo Starting Discord bot...
echo.
python main.py
echo.
echo Bot stopped (or crashed). Press any key to close.
pause > nul
