@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment was not found.
    echo Run setup.bat first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo .env file was not found.
    echo Create .env and add BOT_TOKEN=your_new_token
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
echo Starting Znestrumko bot...
echo Press Ctrl+C to stop the bot.
echo.
python bot.py

echo.
echo Bot stopped.
pause
