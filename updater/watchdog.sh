#!/bin/sh
# Watchdog dell'updater: ogni minuto, DALL'ESTERNO, verifica che il container
# updater sia acceso e usi l'immagine più recente; altrimenti lo (ri)avvia.
# Serve perché un container non può ricreare se stesso: il comando morirebbe a metà.
cd "${PROJECT_DIR:?PROJECT_DIR mancante}" || exit 1
INTERVAL="${WATCHDOG_INTERVAL_SEC:-60}"
log() { echo "$(date '+%F %T') [watchdog] $*"; }
log "attivo"
while true; do
  sleep "$INTERVAL"
  busy=data/.updater_busy   # l'updater sta installando: non disturbare (al massimo 30 min)
  if [ -f "$busy" ] && [ $(( $(date +%s) - $(stat -c %Y "$busy") )) -lt 1800 ]; then continue; fi
  cid=$(docker compose ps -q --status running updater 2>/dev/null | head -n 1)
  if [ -z "$cid" ]; then
    log "updater non in esecuzione: lo riavvio"
    docker compose up -d --no-deps updater 2>&1 | tail -n 2
    continue
  fi
  latest=$(docker image inspect -f '{{.Id}}' "${UPDATER_IMAGE:-tradebot-updater:latest}" 2>/dev/null)
  current=$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null)
  if [ -n "$latest" ] && [ "$latest" != "$current" ]; then
    log "nuova versione dell'updater disponibile: lo ricreo"
    docker compose up -d --no-deps updater 2>&1 | tail -n 2
  fi
done
