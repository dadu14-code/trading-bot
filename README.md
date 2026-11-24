# TradeBot – sistema di trading automatico a 3 bot

> ⚠️ **Software sperimentale. Nessun sistema garantisce profitti.** Parte su conto **DEMO**: si passa al
> reale solo quando la checklist settimanale è tutta verde, e comunque con capitale che puoi permetterti di perdere.

## Architettura

```
                ┌─────────────────────────┐
                │  BOT 1 · STRATEGIA      │  ogni 4h + 07:40
                │  "trader senior" Claude │  regime 1D/4H/1H, analogie storiche,
                │                         │  diario + lezioni → direttiva (bias, rischio)
                └───────────┬─────────────┘  07:15 postmortem → nuove lezioni
                            │ SQLite condiviso (data/tradebot.db)
                ┌───────────▼─────────────┐
                │  BOT 2 · TRADER         │  ogni 5 min, 24/7
                │  esecuzione + rischio   │  segnali, sizing, trailing stop,
                │                         │  circuit breaker, ordini su Alpaca
                └───────────┬─────────────┘
                ┌───────────▼─────────────┐
                │  BOT 3 · REPORTER       │  Telegram: alert live, 08:00 piano,
                │                         │  21:30 consuntivo, domenica settimanale
                └─────────────────────────┘
```

### Come "ragiona"
| Livello | Cosa fa | File |
|---|---|---|
| **Temporale** | Trend primario 1D (SMA200, ADX), intermedio 4H (EMA, struttura massimi/minimi), timing 1H (pullback/breakout) | `tradebot/regime.py` |
| **Memoria del mercato** | Cerca nel passato le 40 situazioni più simili a oggi e misura cosa è successo dopo (rendimento atteso, win-rate, escursione avversa) | `tradebot/patterns.py` |
| **Pattern** | Engulfing, hammer, shooting star, inside bar come conferme | `tradebot/patterns.py` |
| **Esperienza** | Claude, con persona da trader con 30 anni di esperienza, legge tutto + diario + lezioni e decide bias/convinzione/rischio per asset | `tradebot/llm.py` |
| **Memoria propria** | Ogni trade chiuso viene analizzato (buona decisione vs fortuna); le lezioni tornano nel ragionamento futuro | `bots/strategist.py` |
| **Disciplina** | Veti rigidi: mai contro il trend 1D, mai prezzo ipercomprato, mai senza stop; rischio 0,75%/trade; tetti di esposizione; circuit breaker giornaliero/settimanale; kill switch al -15%; pausa dopo 3 perdite di fila | `tradebot/signals.py`, `tradebot/risk.py` |

Claude **non piazza ordini**: orienta. I limiti di rischio sono nel codice e non sono scavalcabili.
Senza chiave Anthropic il sistema funziona lo stesso, con regole di riserva più prudenti.

## Setup (≈20 minuti)

1. **Conto demo Alpaca** (gratis): registrati su <https://alpaca.markets>, passa a *Paper Trading*, genera le API key.
   Copre crypto (24/7) e azioni/ETF USA.
2. **Bot Telegram**: su Telegram scrivi a `@BotFather` → `/newbot` → copia il token. Manda un messaggio
   al tuo bot, poi apri `https://api.telegram.org/bot<TOKEN>/getUpdates` e copia `chat.id`.
3. **Claude API**: chiave su <https://console.anthropic.com>.
4. Configura:
   ```bash
   cp .env.example .env   # e compila le chiavi
   ```
5. **Avvio 24/7** su un server (VPS Linux da ~5 €/mese, es. Hetzner/Contabo/DigitalOcean) con Docker:
   ```bash
   docker compose up -d --build
   docker compose logs -f trader
   ```
   `restart: always` riavvia i bot se crashano o se il server si riavvia.

## Comandi Telegram
`/status` · `/piano` (report "cosa sto facendo") · `/report` (report "cosa ho fatto") · `/settimana` ·
`/lezioni` · `/pausa` (blocca nuovi ingressi, gli stop restano attivi) · `/riprendi`

## Prima di fidarsi: backtest
```bash
pip install -r requirements.txt
python -m tradebot.backtest --symbol BTC/USD --days 730
python -m pytest -q tests
```
Il backtest testa solo la parte quantitativa (senza Claude), commissioni incluse.

## Dal demo al reale
Il report domenicale contiene la checklist: ≥8 settimane, ≥40 operazioni, profit factor ≥1,3,
expectancy positiva, drawdown <10%. Quando è tutta verde: `ALPACA_PAPER=false` con capitale **ridotto**.

## Costi indicativi
- Alpaca: 0 (demo). Commissioni crypto sul reale ~0,15–0,25% per lato.
- Claude (Opus 5.5): ~7 analisi/giorno + postmortem → circa 0,5–1,5 $/giorno.
- VPS: ~5 €/mese.

## Parametri
Tutto in `config/settings.yaml` (universo, rischio, soglie, orari). Dopo ogni modifica: backtest, poi
`docker compose restart`.
