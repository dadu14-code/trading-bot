"""Memoria condivisa fra i 3 bot (SQLite in modalità WAL).

Tabelle:
  directives  – output del bot Strategia (bias per simbolo, visione di mercato)
  trades      – diario completo di ogni operazione (motivazione, MAE/MFE, esito)
  lessons     – lezioni apprese dai postmortem: rientrano nel ragionamento futuro
  equity      – curva del capitale (snapshot periodici)
  events      – coda di notifiche verso Telegram
  kv          – stato (pausa, cooldown, ecc.)
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import ENV

SCHEMA = """
CREATE TABLE IF NOT EXISTS directives(
  id INTEGER PRIMARY KEY, ts TEXT, source TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY, symbol TEXT, asset_class TEXT, side TEXT,
  qty REAL, entry_price REAL, entry_ts TEXT, stop REAL, initial_stop REAL,
  highest REAL, lowest REAL, score REAL, rationale TEXT,
  exit_price REAL, exit_ts TEXT, exit_reason TEXT, pnl REAL, r_multiple REAL,
  status TEXT DEFAULT 'open', postmortem_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS lessons(
  id INTEGER PRIMARY KEY, ts TEXT, text TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS equity(
  id INTEGER PRIMARY KEY, ts TEXT, equity REAL, cash REAL, exposure REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, text TEXT, sent INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS llm_usage(
  id INTEGER PRIMARY KEY, ts TEXT, purpose TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL);
CREATE TABLE IF NOT EXISTS shadow_trades(
  id INTEGER PRIMARY KEY, variant TEXT, symbol TEXT, qty REAL, entry_price REAL, entry_ts TEXT,
  stop REAL, initial_stop REAL, highest REAL, lowest REAL,
  exit_price REAL, exit_ts TEXT, exit_reason TEXT, pnl REAL, r_multiple REAL, status TEXT DEFAULT 'open');
"""
MIGRATIONS = ["ALTER TABLE trades ADD COLUMN backup_order_id TEXT",
              "ALTER TABLE trades ADD COLUMN strategy TEXT DEFAULT 'trend'"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn(path: str | None = None):
    c = sqlite3.connect(path or ENV.db_path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init(path: str | None = None):
    with conn(path) as c:
        c.executescript(SCHEMA)
        for m in MIGRATIONS:  # aggiornamenti dello schema su DB già esistenti
            try:
                c.execute(m)
            except sqlite3.OperationalError:
                pass


# ── strategie ombra ──────────────────────────────────────────────
def shadow_open(variant: str):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM shadow_trades WHERE status='open' AND variant=?", (variant,))]


def shadow_insert(**kw):
    with conn() as c:
        c.execute(f"INSERT INTO shadow_trades({','.join(kw)}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))


def shadow_update(tid: int, **kw):
    with conn() as c:
        c.execute(f"UPDATE shadow_trades SET {','.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), tid))


def shadow_closed(since_iso: str = "1970"):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM shadow_trades WHERE status='closed' AND exit_ts>=?", (since_iso,))]


# ── key/value ────────────────────────────────────────────────────
def kv_get(k, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(k, v):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))


def is_paused() -> bool:
    return bool(kv_get("paused", False))


def cooldown_active() -> bool:
    return time.time() < float(kv_get("cooldown_until", 0))


# ── eventi / notifiche ───────────────────────────────────────────
def event(kind: str, text: str):
    with conn() as c:
        c.execute("INSERT INTO events(ts,kind,text) VALUES(?,?,?)", (now_iso(), kind, text))


def pending_events():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events WHERE sent=0 ORDER BY id")]


def mark_sent(eid: int):
    with conn() as c:
        c.execute("UPDATE events SET sent=1 WHERE id=?", (eid,))


# ── direttive strategiche ────────────────────────────────────────
def save_directive(payload: dict, source: str):
    with conn() as c:
        c.execute("INSERT INTO directives(ts,source,payload) VALUES(?,?,?)",
                  (now_iso(), source, json.dumps(payload)))


def latest_directive() -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM directives ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    d = json.loads(r["payload"])
    d["_ts"], d["_source"] = r["ts"], r["source"]
    return d


# ── diario operazioni ────────────────────────────────────────────
def open_trade(**kw) -> int:
    kw.setdefault("entry_ts", now_iso())
    kw.setdefault("highest", kw["entry_price"])
    kw.setdefault("lowest", kw["entry_price"])
    kw.setdefault("initial_stop", kw["stop"])
    cols = ",".join(kw)
    with conn() as c:
        cur = c.execute(f"INSERT INTO trades({cols}) VALUES({','.join('?' * len(kw))})",
                        tuple(kw.values()))
        return cur.lastrowid


def update_trade(tid: int, **kw):
    sets = ",".join(f"{k}=?" for k in kw)
    with conn() as c:
        c.execute(f"UPDATE trades SET {sets} WHERE id=?", (*kw.values(), tid))


def open_trades():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM trades WHERE status='open'")]


def closed_trades(since_iso: str | None = None, limit: int = 200):
    q, args = "SELECT * FROM trades WHERE status='closed'", []
    if since_iso:
        q += " AND exit_ts>=?"
        args.append(since_iso)
    q += " ORDER BY exit_ts DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def trades_opened_since(since_iso: str):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE entry_ts>=? ORDER BY entry_ts", (since_iso,))]


# ── lezioni ──────────────────────────────────────────────────────
def add_lesson(text: str, source: str = "postmortem"):
    with conn() as c:
        c.execute("INSERT INTO lessons(ts,text,source) VALUES(?,?,?)", (now_iso(), text, source))


def recent_lessons(n: int = 25):
    with conn() as c:
        return [r["text"] for r in c.execute("SELECT text FROM lessons ORDER BY id DESC LIMIT ?", (n,))]


# ── equity ───────────────────────────────────────────────────────
def snapshot_equity(equity: float, cash: float, exposure: float):
    with conn() as c:
        c.execute("INSERT INTO equity(ts,equity,cash,exposure) VALUES(?,?,?,?)",
                  (now_iso(), equity, cash, exposure))


def equity_since(since_iso: str):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM equity WHERE ts>=? ORDER BY ts", (since_iso,))]


def peak_equity() -> float | None:
    with conn() as c:
        r = c.execute("SELECT MAX(equity) m FROM equity").fetchone()
    return r["m"] if r else None


# ── consumi Claude ───────────────────────────────────────────────
def record_llm_usage(purpose: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float):
    with conn() as c:
        c.execute("INSERT INTO llm_usage(ts,purpose,model,input_tokens,output_tokens,cost_usd) VALUES(?,?,?,?,?,?)",
                  (now_iso(), purpose, model, input_tokens, output_tokens, cost_usd))


def llm_usage_since(since_iso: str = "1970") -> dict:
    with conn() as c:
        r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
                      "COALESCE(SUM(cost_usd),0) c FROM llm_usage WHERE ts>=?", (since_iso,)).fetchone()
    return {"calls": r["n"], "input_tokens": r["i"], "output_tokens": r["o"], "cost_usd": r["c"]}


# ── azzeramento del conto demo ───────────────────────────────────
def last_equity() -> float | None:
    with conn() as c:
        r = c.execute("SELECT equity FROM equity ORDER BY id DESC LIMIT 1").fetchone()
    return r["equity"] if r else None


def archive_account_history(reason: str):
    """Il conto è stato azzerato/ricaricato sul broker: archivia la curva dell'equity,
    annulla le operazioni rimaste aperte e azzera lo stato di rischio. Diario, lezioni
    e costi Claude restano."""
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS equity_archive AS SELECT * FROM equity WHERE 0")
        c.execute("INSERT INTO equity_archive SELECT * FROM equity")
        c.execute("DELETE FROM equity")
        c.execute("UPDATE trades SET status='void', exit_reason=?, exit_ts=? WHERE status='open'", (reason, now_iso()))
        for k in ("cb_state", "cooldown_until", "paused"):
            c.execute("DELETE FROM kv WHERE k=?", (k,))
