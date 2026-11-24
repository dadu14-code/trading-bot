@echo off
REM Avvia i 3 bot in Docker (serve Docker Desktop acceso)
cd /d "%~dp0"
if not exist .env ( copy .env.example .env >nul & echo Ho creato .env: inserisci le chiavi e rilancia. & notepad .env & exit /b )
docker compose run --rm --build tools python -m tradebot.check || ( echo. & echo Diagnostica fallita: controlla .env & pause & exit /b )
docker compose up -d --build
echo.
echo Bot avviati. Chiudi questa finestra quando vuoi: i bot restano attivi.
docker compose logs -f --tail 50
