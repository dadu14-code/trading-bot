@echo off
REM Primo avvio: crea l'ambiente e installa le librerie. Poi lancia i 3 bot.
cd /d "%~dp0"
if not exist .venv (
  py -3.12 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python run_all.py
pause
