#!/usr/bin/env bash
# Aggiornamento automatico dei bot.
# Ogni UPDATE_INTERVAL_SEC controlla GitHub; se c'è una versione nuova:
#   1. ti scrive su Telegram COSA cambia e PERCHÉ (dai messaggi di commit)
#   2. scarica il codice, ricostruisce l'immagine ed esegue i test
#   3. test ok  → riavvia i bot con la nuova versione e conferma
#      test ko  → torna alla versione precedente e ti avvisa
# .env e data/ (chiavi, diario, lezioni, equity) non vengono mai toccati.
set -u
cd "${PROJECT_DIR:?PROJECT_DIR mancante}"
INTERVAL="${UPDATE_INTERVAL_SEC:-300}"
BRANCH="${GITHUB_BRANCH:-main}"
log() { echo "$(date '+%F %T') [updater] $*"; }

remote_url() {
  if [ -n "${GIT_REMOTE_URL:-}" ]; then echo "$GIT_REMOTE_URL"
  elif [ -n "${GITHUB_TOKEN:-}" ]; then echo "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git"
  else echo "https://github.com/${GITHUB_REPO}.git"; fi
}

tg() {
  [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && { log "TG: $1"; return; }
  curl -s -m 20 -o /dev/null -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=${1:0:3900}" \
    --data-urlencode "disable_web_page_preview=true" || log "invio Telegram fallito"
}

changelog() {  # cosa + perché, senza le righe tecniche di firma
  git log --no-merges --format='▪️ %s%n%b' "$@" 2>/dev/null \
    | grep -v -E '^(Co-Authored-By|Claude-Session|Signed-off-by):' | sed '/^[[:space:]]*$/d' | head -c 3000
}

if [ -z "${GITHUB_REPO:-}" ] && [ -z "${GIT_REMOTE_URL:-}" ]; then
  log "GITHUB_REPO non impostato nel file .env: aggiornamenti automatici disattivati"
  exec sleep infinity
fi
git config --global --add safe.directory "$PWD"
git config --global user.email "updater@tradebot.local"; git config --global user.name "updater"
[ -d .git ] || git init -q
last_err=0

while true; do
  if git fetch -q "$(remote_url)" "$BRANCH" 2>/tmp/fetch.err; then
    NEW=$(git rev-parse FETCH_HEAD)
    CUR=$(git rev-parse -q --verify HEAD 2>/dev/null || echo "")
    if [ "$NEW" != "$CUR" ]; then
      if [ -n "$CUR" ] && git merge-base --is-ancestor "$CUR" "$NEW" 2>/dev/null; then RANGE="$CUR..$NEW"; else RANGE="-5 $NEW"; fi
      # shellcheck disable=SC2086
      CHANGES=$(changelog $RANGE)
      log "nuova versione ${NEW:0:7} (da ${CUR:0:7})"
      tg "🔄 Aggiornamento dei bot in arrivo (${NEW:0:7})

Cosa cambia e perché:
${CHANGES}

Eseguo i test prima di installarlo…"
      git reset -q --hard "$NEW"
      if docker compose build -q >/tmp/build.log 2>&1 && \
         docker compose run --rm -T tools python -m pytest -q tests >/tmp/test.log 2>&1; then
        docker compose up -d strategist trader reporter >/tmp/up.log 2>&1
        tg "✅ Aggiornamento ${NEW:0:7} installato: test superati, bot riavviati."
        log "installata ${NEW:0:7}"
        docker compose up -d --build updater >/dev/null 2>&1   # se è cambiato anche l'updater, si aggiorna per ultimo
      else
        FAIL=$(tail -q -n 15 /tmp/build.log /tmp/test.log 2>/dev/null | tail -c 1200)
        if [ -n "$CUR" ]; then
          git reset -q --hard "$CUR"
          docker compose build -q >/dev/null 2>&1
        fi
        tg "❌ Aggiornamento ${NEW:0:7} NON installato: i test non sono passati. I bot restano sulla versione precedente (${CUR:0:7}).

${FAIL}"
        log "test falliti, rollback a ${CUR:0:7}"
        # non riprovare la stessa versione rotta a ogni giro
        [ -n "${UPDATER_ONCE:-}" ] && break
        while [ "$(git ls-remote "$(remote_url)" "refs/heads/$BRANCH" 2>/dev/null | cut -f1)" = "$NEW" ]; do sleep "$INTERVAL"; done
      fi
    fi
  else
    now=$(date +%s)
    log "controllo aggiornamenti fallito: $(head -c 200 /tmp/fetch.err | sed 's/x-access-token:[^@]*@/***@/')"
    if [ $((now - last_err)) -gt 21600 ]; then
      tg "⚠️ Non riesco a controllare gli aggiornamenti su GitHub (token scaduto o rete). I bot continuano a funzionare."
      last_err=$now
    fi
  fi
  [ -n "${UPDATER_ONCE:-}" ] && break   # usato solo dai test
  sleep "$INTERVAL"
done
