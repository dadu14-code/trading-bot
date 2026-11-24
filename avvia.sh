#!/usr/bin/env bash
# Avvia i 3 bot in Docker (Mac/Linux/VPS)
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then cp .env.example .env; echo "Ho creato .env: inserisci le chiavi e rilancia."; exit 1; fi
docker compose run --rm --build tools python -m tradebot.check || { echo "Diagnostica fallita: controlla .env"; exit 1; }
docker compose up -d --build
echo "Bot avviati. Ctrl+C chiude solo i log: i bot restano attivi."
docker compose logs -f --tail 50
