@echo off
setlocal

cd /d "%~dp0"

if not exist ".env" (
    echo [WARNING] No .env file found. Copying .env.example to .env ...
    copy .env.example .env
    echo [IMPORTANT] Please edit the .env file with your PostgreSQL password before proceeding!
    notepad .env
)

echo Starting TANZANITESAT Ground Station...
python run.py
pause
