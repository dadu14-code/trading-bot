# TradeBot – sistema di trading automatico

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
                ┌─────────────────────────┐
                │  BOT 4 · UPDATER        │  ogni 5 min controlla GitHub: avvisa su Telegram
                │                         │  cosa cambia e perché → test → installa o annulla
                └─────────────────────────┘
```

### Le tre strategie
| Strategia | Quando opera | Rischio |
|---|---|---|
| 🔵 **Trend long** | Trend rialzista su 1D e 4H, pullback o breakout su 1H, analogie storiche favorevoli, bias "long" | 0,75% × convinzione |
| 🟣 **Short ETF** (SPY, QQQ, GLD) | Trend ribassista su 1D e 4H, rimbalzo fallito o rottura dei minimi, **solo con bias "short"** dal bot Strategia. Niente short sulle crypto (Alpaca non lo consente) | 0,75% × convinzione × 0,75 |
| 🟡 **Rimbalzo da capitolazione** | Crollo esteso (≥2,5 ATR sotto la media o RSI giornaliero <25) + volume ≥2× + prima inversione; per le crypto anche paura estrema. Claude può vietarlo | 0,25% fisso, obiettivo +1,5R, massimo 2 giorni, pausa di 48h dopo ogni rimbalzo |

### Come "ragiona"
| Livello | Cosa fa | File |
|---|---|---|
| **Temporale** | Trend primario 1D (SMA200, ADX), intermedio 4H (EMA, struttura massimi/minimi), timing 1H (pullback/breakout) | `tradebot/regime.py` |
| **Memoria del mercato** | Cerca nel passato le 40 situazioni più simili a oggi e misura cosa è successo dopo (rendimento atteso, win-rate, escursione avversa) | `tradebot/patterns.py` |
| **Pattern** | Engulfing, hammer, shooting star, inside bar come conferme | `tradebot/patterns.py` |
| **Esperienza** | Claude, con persona da trader con 30 anni di esperienza, legge tutto + diario + lezioni e decide bias/convinzione/rischio per asset | `tradebot/llm.py` |
| **Memoria propria** | Ogni trade chiuso viene analizzato (buona decisione vs fortuna); le lezioni tornano nel ragionamento futuro | `bots/strategist.py` |
| **Disciplina** | Veti rigidi: mai contro il trend 1D, mai prezzo ipercomprato, mai senza stop; rischio 0,75%/trade; tetti di esposizione; circuit breaker giornaliero/settimanale; kill switch al -15%; **pausa di 8h dopo 3 perdite di fila** | `tradebot/signals.py`, `tradebot/risk.py` |
| **Macro** | Nessun nuovo ingresso da 2h prima a 1h dopo eventi USA ad alto impatto (FOMC, CPI, NFP); Fear & Greed e funding rate crypto passati alla Strategia | `tradebot/macro.py` |
| **Volatility targeting** | Se il portafoglio oscilla più del 25% annuo, il rischio di ogni nuova operazione scala giù in proporzione | `tradebot/risk.py` |
| **Stop di emergenza** | Oltre allo stop software, uno stop vive sui server Alpaca (1 ATR più sotto): protegge anche se il PC si spegne | `tradebot/broker.py` |
| **Strategie ombra** | 3 varianti di parametri girano in simulazione sugli stessi dati; il report domenicale le confronta con la reale | `bots/trader.py` |
| **Watchdog** | Se il Trader non dà segni di vita per 20 minuti ti arriva un allarme su Telegram | `bots/reporter.py` |

Claude **non piazza ordini**: orienta. I limiti di rischio sono nel codice e non sono scavalcabili.
Senza chiave Anthropic il sistema funziona lo stesso, con regole di riserva più prudenti.

## Setup (≈20 minuti)

1. **Conto demo Alpaca** (gratis): registrati su <https://alpaca.markets>, passa a *Paper Trading*, genera le API key.
   Copre crypto (24/7) e azioni/ETF USA.
2. **Bot Telegram**: su Telegram scrivi a `@BotFather` → `/newbot` → copia il token. Manda un messaggio
   al tuo bot, poi apri `https://api.telegram.org/bot<TOKEN>/getUpdates` e copia `chat.id`.
3. **Claude API**: chiave su <https://console.anthropic.com>.
4. Installa **Docker Desktop** (<https://www.docker.com/products/docker-desktop/>). Su Windows accetta
   l'attivazione di WSL2 se richiesta e riavvia. Nelle impostazioni attiva
   *Start Docker Desktop when you sign in*. Non serve installare Python.

### Fase 1 – sul tuo PC
- **Windows**: doppio clic su `avvia.bat`. Al primo avvio crea `.env` e lo apre nel Blocco note:
  inserisci le chiavi, salva e rilancia `avvia.bat`.
- **Mac/Linux**: `./avvia.sh` (stesso funzionamento).

Lo script costruisce l'immagine, esegue la diagnostica e avvia i 3 container in background. Puoi chiudere
la finestra: i bot restano attivi e ripartono da soli dopo un crash o un riavvio del PC (`restart: always`).
Per fermarli: `ferma.bat` / `./ferma.sh`. I dati e i log restano nella cartella `data/`.

**Disattiva la sospensione del PC** (Windows: Impostazioni → Sistema → Alimentazione → Sospensione: *Mai*).
Se il PC si spegne, il watchdog ti avvisa su Telegram e gli stop di emergenza sul broker restano attivi.

### Aggiornamenti automatici
Il 4° container (`updater`) controlla il repository GitHub ogni 5 minuti. Quando c'è una versione nuova:
1. ti scrive su Telegram **cosa cambia e perché**;
2. scarica il codice, ricostruisce l'immagine ed esegue tutti i test;
3. se passano riavvia i bot e conferma; se falliscono **torna alla versione precedente** e ti avvisa.

`.env` e `data/` (chiavi, diario, lezioni, curva dell'equity) non vengono mai toccati.

Configurazione una tantum: un token GitHub di sola lettura in `.env`:
1. GitHub → foto profilo → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token**.
2. Nome `tradebot-updater`; scadenza a piacere (quando scade ricevi un avviso su Telegram);
   **Repository access: Only select repositories → `trading-bot`**.
3. **Permissions → Repository permissions → Contents: Read-only**. Nient'altro.
4. *Generate token*, copialo e incollalo in `.env` alla riga `GITHUB_TOKEN=`.

### Fase 2 – VPS
Stessa identica immagine. Su un VPS Linux (~5 €/mese) con Docker installato:
```bash
git clone https://github.com/dadu14-code/trading-bot.git && cd trading-bot
```
Copia il tuo `.env` e la cartella `data/` dal PC (così il bot conserva diario, lezioni e curva dell'equity), poi:
```bash
./avvia.sh
```
Ferma prima i bot sul PC: due istanze sullo stesso conto si pesterebbero i piedi.

### Comandi utili
```bash
docker compose ps
```
```bash
docker compose logs -f trader
```
```bash
docker compose run --rm tools python -m tradebot.check --telegram-test
```
Dopo una modifica a `config/settings.yaml`:
```bash
docker compose restart
```
Dopo una modifica al codice:
```bash
docker compose up -d --build
```

## Comandi Telegram
`/status` · `/piano` (report "cosa sto facendo") · `/report` (report "cosa ho fatto") · `/settimana` ·
`/lezioni` · `/check` (diagnostica) · `/pausa` (blocca nuovi ingressi, gli stop restano attivi) · `/riprendi`

## Prima di fidarsi: backtest e ottimizzazione walk-forward
```bash
docker compose run --rm tools python -m tradebot.backtest --symbol BTC/USD --days 730
```
```bash
docker compose run --rm tools python -m tradebot.backtest --symbol SPY --days 730 --strategy all
```
```bash
docker compose run --rm tools python -m tradebot.optimize --symbols BTC/USD ETH/USD SOL/USD --days 730
```
```bash
docker compose run --rm tools python -m pytest -q tests
```
Il backtest testa solo la parte quantitativa (senza Claude), con commissioni e senza look-ahead.
L'ottimizzatore sceglie i parametri sul passato e li misura su periodi **mai visti**, preferendo
combinazioni robuste (i cui vicini funzionano anch'essi). Scrive `config/optimized.yaml`: applica i
valori in `settings.yaml` solo se il risultato fuori campione batte i parametri attuali.

## Dal demo al reale
Il report domenicale contiene la checklist: ≥8 settimane, ≥40 operazioni, profit factor ≥1,3,
expectancy positiva, drawdown <10%. Quando è tutta verde: `ALPACA_PAPER=false` con capitale **ridotto**.

## Costi indicativi
- Alpaca: 0 (demo). Commissioni crypto sul reale ~0,15–0,25% per lato.
- Claude (Opus 5.5): ~7 analisi/giorno + postmortem → circa 0,5–1,5 $/giorno. L'API si paga a parte
  rispetto all'abbonamento Claude Pro. Per spendere meno: `CLAUDE_MODEL=claude-sonnet-5-5` nel `.env`
  oppure `strategist_every_hours: 8`. Senza chiave il bot usa le regole di riserva.
- VPS: ~5 €/mese.

## Parametri
Tutto in `config/settings.yaml` (universo, rischio, soglie, orari). Dopo ogni modifica: backtest, poi
`docker compose restart`.
