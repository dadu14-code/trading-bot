"""Scala di capitali: "quale capitale serve per guadagnare davvero?".

Ogni operazione REALE del bot (stessi prezzi, stessi istanti) viene replicata su
conti virtuali di diverse dimensioni, con i vincoli che quel capitale avrebbe davvero:
  • short solo con conto margin (su Alpaca: ≥ 2.000 $) e solo in azioni intere
  • long su azioni/ETF anche in frazioni, ordine minimo 1 $
  • commissioni/spread realistici (crypto ~0,25% per lato, ETF ~0,05%)
Il costo di Claude è lo stesso per tutti i capitali: è un costo fisso.
"""
import math
from datetime import datetime, timezone

from . import db

DEFAULT_LEVELS = [100, 500, 1000, 2500, 5000, 10000, 25000]
FEES = {"crypto": 0.0025, "stock": 0.0005}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ladder_trades(
  id INTEGER PRIMARY KEY, trade_id INTEGER, level_eur REAL, symbol TEXT, side TEXT, strategy TEXT,
  asset_class TEXT, qty REAL, entry_price REAL, status TEXT, skip_reason TEXT,
  exit_price REAL, pnl REAL, opened_ts TEXT, closed_ts TEXT);
"""


def _cfg(cfg: dict) -> dict:
    lc = cfg.get("capital_ladder", {})
    rp = cfg.get("report", {})
    return {"levels": lc.get("levels_eur", DEFAULT_LEVELS), "fx": rp.get("usd_per_eur", 1.08),
            "margin_min": lc.get("margin_min_usd", 2000), "min_order": lc.get("min_order_usd", 1.0),
            "fee_crypto": (float(cfg.get("paper", {}).get("fee_rate", 0.0005)) + float(cfg.get("paper", {}).get("slippage_bps", 5)) / 10_000)
                          if cfg.get("broker") == "kraken_paper" else FEES["crypto"]}


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)
    if not db.kv_get("ladder_start"):
        db.kv_set("ladder_start", db.now_iso())


def _rows(sql: str, args=()):
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def level_equity_usd(level_eur: float, fx: float) -> float:
    r = _rows("SELECT COALESCE(SUM(pnl),0) p FROM ladder_trades WHERE level_eur=? AND status='closed'", (level_eur,))
    return level_eur * fx + r[0]["p"]


def _equity_at(ts: str, fallback: float) -> float:
    r = _rows("SELECT equity FROM equity WHERE ts<=? ORDER BY ts DESC LIMIT 1", (ts,))
    return r[0]["equity"] if r else fallback


def sync(real_equity: float, cfg: dict):
    """Replica le aperture/chiusure reali non ancora replicate. Idempotente: va chiamata a ogni ciclo."""
    init()
    k = _cfg(cfg)
    start = db.kv_get("ladder_start")
    done = {r["trade_id"] for r in _rows("SELECT DISTINCT trade_id FROM ladder_trades")}
    todo = _rows("SELECT * FROM trades WHERE (status='open' OR entry_ts>=?) AND status!='void'", (start,))
    for t in todo:
        if t["id"] in done:
            continue
        eq_then = _equity_at(t["entry_ts"], real_equity)
        frac = t["qty"] * t["entry_price"] / eq_then          # peso della posizione sul conto reale
        for lv in k["levels"]:
            eq = level_equity_usd(lv, k["fx"])
            notional = frac * eq
            qty, reason = notional / t["entry_price"], None
            if t["side"] == "short" and t["asset_class"] == "stock":   # i perpetui crypto non richiedono margin
                if eq < k["margin_min"]:
                    reason = f"short: serve un conto margin ≥ {k['margin_min']:,.0f} $"
                else:
                    qty = math.floor(qty)
                    if qty < 1:
                        reason = "short: anche 1 azione intera è troppo grande per questo capitale"
            if not reason and qty * t["entry_price"] < k["min_order"]:
                reason = f"ordine sotto il minimo di {k['min_order']:g} $"
            with db.conn() as c:
                c.execute("INSERT INTO ladder_trades(trade_id,level_eur,symbol,side,strategy,asset_class,qty,entry_price,"
                          "status,skip_reason,opened_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (t["id"], lv, t["symbol"], t["side"], t.get("strategy") or "trend", t["asset_class"],
                           0 if reason else qty, t["entry_price"], "skipped" if reason else "open", reason, t["entry_ts"]))
    # chiusure: stesso prezzo d'uscita del conto reale, con commissioni
    for m in _rows("SELECT l.*, t.exit_price tex, t.exit_ts tts, t.status tst FROM ladder_trades l "
                   "JOIN trades t ON t.id=l.trade_id WHERE l.status='open' AND t.status IN ('closed','void')"):
        if m["tst"] == "void" or m["tex"] is None:   # annullata o senza prezzo d'uscita: non conteggiabile
            with db.conn() as c:
                c.execute("UPDATE ladder_trades SET status='void' WHERE id=?", (m["id"],))
            continue
        sgn = -1 if m["side"] == "short" else 1
        fee = k["fee_crypto"] if m["asset_class"] == "crypto" else FEES["stock"]
        pnl = sgn * (m["tex"] - m["entry_price"]) * m["qty"] - fee * (m["tex"] + m["entry_price"]) * m["qty"]
        with db.conn() as c:
            c.execute("UPDATE ladder_trades SET status='closed', exit_price=?, pnl=?, closed_ts=? WHERE id=?",
                      (m["tex"], pnl, m["tts"], m["id"]))


def summary(cfg: dict, price_fn=None) -> dict:
    """Risultato per capitale: guadagno lordo, costo Claude (fisso), netto, capitale di pareggio stimato."""
    init()
    k = _cfg(cfg)
    start = db.kv_get("ladder_start")
    days = max((datetime.now(timezone.utc) - datetime.fromisoformat(start)).total_seconds() / 86400, 1 / 24)
    claude_eur = db.llm_usage_since(start)["cost_usd"] / k["fx"]
    prices = {}
    out = []
    for lv in k["levels"]:
        rows = _rows("SELECT * FROM ladder_trades WHERE level_eur=?", (lv,))
        realized = sum(r["pnl"] or 0 for r in rows if r["status"] == "closed")
        unreal = 0.0
        for r in rows:
            if r["status"] == "open" and price_fn:
                if r["symbol"] not in prices:
                    try:
                        prices[r["symbol"]] = price_fn(r["symbol"])
                    except Exception:
                        prices[r["symbol"]] = r["entry_price"]
                unreal += (-1 if r["side"] == "short" else 1) * (prices[r["symbol"]] - r["entry_price"]) * r["qty"]
        gross_eur = (realized + unreal) / k["fx"]
        out.append({"level": lv, "gross_eur": gross_eur, "net_eur": gross_eur - claude_eur,
                    "ret_pct": gross_eur / lv, "trades": sum(r["status"] in ("open", "closed") for r in rows),
                    "skipped": sum(r["status"] == "skipped" for r in rows),
                    "skip_reasons": sorted({r["skip_reason"] for r in rows if r["skip_reason"]})})
    # pareggio: capitale C con rendimento%(C) × C = costo Claude. Il rendimento dipende dalla taglia:
    # sotto la soglia margin niente short (rendimento dei capitali piccoli), sopra tutte le strategie.
    small = [lv for lv in out if lv["level"] * k["fx"] < k["margin_min"]]
    rs = small[-1]["ret_pct"] if small else 0
    rb = out[-1]["ret_pct"] if out else 0
    cands = []
    if rs > 0 and claude_eur / rs * k["fx"] < k["margin_min"]:
        cands.append(claude_eur / rs)
    if rb > 0:
        cands.append(max(claude_eur / rb, k["margin_min"] / k["fx"]))
    breakeven = min(cands) if cands else None
    return {"days": days, "claude_eur": claude_eur, "claude_month_eur": claude_eur / max(days, 1) * 30,
            "levels": out, "breakeven_eur": breakeven, "since": start}
