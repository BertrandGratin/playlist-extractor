#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Première installation..."
  python3 -m venv .venv || { echo "Python 3 introuvable : installe-le depuis https://www.python.org/downloads/"; read; exit 1; }
fi
source .venv/bin/activate
python -m pip install -q --upgrade -r requirements.txt
python app.py
