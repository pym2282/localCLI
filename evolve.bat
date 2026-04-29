@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "Origin\main.py" (
    echo [Origin not found. Creating baseline...]
    .venv\Scripts\python.exe compare.py baseline
)

.venv\Scripts\python.exe improve.py
pause
