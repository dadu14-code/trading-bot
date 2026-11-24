"""Avvio locale (PC): lancia i 3 bot e li riavvia se si fermano.

    python run_all.py

Ctrl+C per fermare tutto. Su VPS usa invece docker compose.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

BOTS = ["bots.strategist", "bots.trader", "bots.reporter"]
ROOT = Path(__file__).resolve().parent
env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def start(mod):
    print(f"▶ avvio {mod}", flush=True)
    return subprocess.Popen([sys.executable, "-m", mod], cwd=ROOT, env=env)


def main():
    if not (ROOT / ".env").exists():
        sys.exit("Manca il file .env: copia .env.example in .env e inserisci le chiavi.")
    rc = subprocess.call([sys.executable, "-m", "tradebot.check"], cwd=ROOT, env=env)
    if rc != 0:
        sys.exit("Diagnostica Alpaca fallita: correggi il file .env e riprova.")
    procs = {}
    for m in BOTS:
        procs[m] = start(m)
        time.sleep(3)  # la Strategia scrive la prima direttiva prima che il Trader parta
    restarts = {m: [] for m in BOTS}
    try:
        while True:
            time.sleep(10)
            for m, p in procs.items():
                if p.poll() is not None:
                    restarts[m] = [t for t in restarts[m] if time.time() - t < 3600] + [time.time()]
                    wait = min(300, 10 * 2 ** (len(restarts[m]) - 1))   # backoff se crasha in loop
                    print(f"⚠ {m} terminato (codice {p.returncode}), riavvio tra {wait}s", flush=True)
                    time.sleep(wait)
                    procs[m] = start(m)
    except KeyboardInterrupt:
        print("Arresto dei bot…")
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            p.wait(timeout=15)


if __name__ == "__main__":
    main()
