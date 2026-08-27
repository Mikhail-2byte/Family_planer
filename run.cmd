@echo off
rem cmd reads this file in the OEM codepage, so switch to UTF-8 before any
rem Cyrillic appears below - otherwise the messages come out as garbage.
chcp 65001 >nul

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Нет виртуального окружения. Создайте его:
    echo    python -m venv .venv
    echo    .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

.venv\Scripts\alembic.exe upgrade head
if errorlevel 1 exit /b 1

.venv\Scripts\python.exe -m bot
