@echo off
cd /d "%~dp0"
echo === Installing packages ===
python -m pip install flask requests openai python-dotenv
echo.
echo === Done! Now run run_bot.bat ===
pause
