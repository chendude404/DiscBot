@echo off
rem Runs bot.py forever: if it exits or crashes, wait 15 seconds and start it again.
rem Output goes to data\bot.log. Used by start_bot.ps1 and bot_task.ps1, but can also be run by hand.

cd /d "%~dp0.."
if not exist data mkdir data
set "PYTHON=C:\Users\ayche\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

:loop
echo [%date% %time%] starting bot>> data\bot.log
"%PYTHON%" bot.py >> data\bot.log 2>&1
echo [%date% %time%] bot exited with code %errorlevel%; restarting in 15s>> data\bot.log
rem ping instead of timeout: timeout fails when there is no console (as under Task Scheduler)
ping -n 16 127.0.0.1 > nul
goto loop
