@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Premiere installation...
  python -m venv .venv || (echo Python introuvable. Installe-le depuis https://www.python.org/downloads/ en cochant "Add python.exe to PATH". & pause & exit /b)
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade -r requirements.txt
python app.py
pause
