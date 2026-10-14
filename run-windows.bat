@echo off
cd /d "%~dp0"
if not exist .venv (
  echo First-time setup...
  python -m venv .venv || (echo Python not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b)
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade -r requirements.txt
python app.py
pause
