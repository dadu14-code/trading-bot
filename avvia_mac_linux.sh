#!/usr/bin/env bash
# Primo avvio: crea l'ambiente e installa le librerie. Poi lancia i 3 bot.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt
fi
# su Mac impedisce lo stop del sistema finché i bot girano
if command -v caffeinate >/dev/null; then exec caffeinate -is .venv/bin/python run_all.py; fi
exec .venv/bin/python run_all.py
