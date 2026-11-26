"""Conto SIMULATO sui prezzi VERI di Kraken Futures (perpetui lineari, senza leva).

Serve a fare la prova generale del conto reale da 100 € prima di rischiare soldi:
  • prezzi, candele e funding letti dalle API pubbliche di Kraken (nessuna chiave)
  • eseguito al prezzo di mercato ± slittamento, con la commissione di Kraken
  • quantità arrotondate alla precisione del contratto, ordine minimo rispettato
  • funding dei perpetui addebitato/accreditato ogni ora sulle posizioni aperte
  • niente leva: il nozionale lordo non può superare il capitale

Stato del conto (saldo, posizioni, commissioni, funding) salvato nel database.
SOLO il bot Trader lo modifica (writer=True); gli altri bot lo leggono.
"""
import logging
import time
from datetime import datetime, timezone

import ccxt
import pandas as pd

from . import db

log = logging.getLogger(__name__)
TF = {"1Day": "1d", "4Hour": "4h", "1Hour": "1h"}
TF_MS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000}
LOOKBACK_DAYS = {"1Day": 500, "4Hour": 150, "1Hour": 75}
STATE_KEY = "paper_state"


def norm(symbol: str) -> str:
    return symbol.replace("/", "")


class PaperBroker:
    def __init__(self, cfg: dict, writer: bool = False, exchange=None):
        pc = cfg.get("paper", {})
        rp = cfg.get("report", {})
        self.fee = float(pc.get("fee_rate", 0.0005))
        self.slip = float(pc.get("slippage_bps", 5)) / 10_000
        self.max_lev = float(pc.get("max_leverage", 1.0))
        fx = rp.get("usd_per_eur", 1.08) if rp.get("real_currency", "EUR") == "EUR" else 1.0
        self.start_usd = float(pc.get("start_capital", 100)) * fx
        self.ids = pc.get("markets", {"BTC/USD": "PF_XBTUSD", "ETH/USD": "PF_ETHUSD", "SOL/USD": "PF_SOLUSD"})
        self.writer = writer
        self.ex = exchange or getattr(ccxt, pc.get("exchange", "krakenfutures"))({"enableRateLimit": True, "timeout": 20_000})
        self._loaded = False
        self._cache: dict = {}

    # ── mercati ───────────────────────────────────────────────────
    def _market(self, sym: str) -> dict:
        if not self._loaded:
            self.ex.load_markets()
            self._loaded = True
        mid = self.ids.get(sym)
        found = self.ex.markets_by_id.get(mid) if mid else None
        if isinstance(found, list):
            found = found[0] if found else None
        if found:
            return found
        base = sym.split("/")[0]
        return self.ex.market(f"{base}/USD:USD")

    def _ccxt_symbol(self, sym: str) -> str:
        return self._market(sym)["symbol"]

    def normalize_qty(self, sym: str, qty: float) -> float:
        """Arrotonda PER DIFETTO alla precisione del contratto; 0 se sotto il minimo."""
        m = self._market(sym)
        step = (m.get("precision") or {}).get("amount") or 0.0001
        qty = int(abs(qty) / step + 1e-9) * step
        qty = round(qty, 10)
        min_amt = ((m.get("limits") or {}).get("amount") or {}).get("min") or step
        return qty if qty >= min_amt - 1e-12 else 0.0

    # ── dati di mercato ───────────────────────────────────────────
    def last_price(self, sym: str) -> float:
        key = ("px", sym)
        if key in self._cache and time.time() - self._cache[key][0] < 20:
            return self._cache[key][1]
        t = self.ex.fetch_ticker(self._ccxt_symbol(sym))
        px = float(t.get("last") or t.get("close") or t.get("mark") or t["info"].get("markPrice"))
        self._cache[key] = (time.time(), px)
        return px

    def history(self, sym: str, tf: str, days: int) -> pd.DataFrame:
        since = int((time.time() - days * 86_400) * 1000)
        calls = int(days * 86_400_000 / TF_MS[TF[tf]] / 2000) + 2
        rows = self.ex.fetch_ohlcv(self._ccxt_symbol(sym), TF[tf], since=since,
                                   params={"paginate": True, "paginationCalls": max(calls, 2)})
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
        df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
        return df.sort_index().astype(float)

    def bars(self, sym: str, tf: str) -> pd.DataFrame:
        key = ("bars", sym, tf)
        ttl = {"1Day": 3600, "4Hour": 900, "1Hour": 240}[tf]
        if key in self._cache and time.time() - self._cache[key][0] < ttl:
            return self._cache[key][1]
        df = self.history(sym, tf, LOOKBACK_DAYS[tf])
        self._cache[key] = (time.time(), df)
        return df

    def funding_rates(self) -> dict:
        """Tasso di funding ORARIO relativo al prezzo, per simbolo interno."""
        key = ("funding",)
        if key in self._cache and time.time() - self._cache[key][0] < 600:
            return self._cache[key][1]
        out = {}
        try:
            fr = self.ex.fetch_funding_rates([self._ccxt_symbol(s) for s in self.ids])
            for s in self.ids:
                r = fr.get(self._ccxt_symbol(s)) or {}
                if r.get("fundingRate") is not None:
                    out[s] = float(r["fundingRate"])
        except Exception as e:
            log.info("funding non disponibile: %s", e)
        self._cache[key] = (time.time(), out)
        return out

    # ── stato del conto ───────────────────────────────────────────
    def _state(self) -> dict:
        st = db.kv_get(STATE_KEY)
        if not st:
            st = {"cash": self.start_usd, "positions": {}, "fees": 0.0, "funding": 0.0,
                  "last_funding_ts": time.time(), "created": db.now_iso()}
            if self.writer:
                db.kv_set(STATE_KEY, st)
        return st

    def _save(self, st: dict):
        if self.writer:
            db.kv_set(STATE_KEY, st)

    def _accrue_funding(self, st: dict) -> dict:
        """Funding dalle ultime ore: i long pagano se il tasso è positivo, gli short incassano."""
        now = time.time()
        hours = (now - st.get("last_funding_ts", now)) / 3600
        if hours <= 0 or not st["positions"]:
            st["last_funding_ts"] = now
            return st
        rates = self.funding_rates()
        for sym, p in st["positions"].items():
            rate = rates.get(sym, 0.0)
            pay = p["qty"] * self.last_price(sym) * rate * hours     # qty<0 (short) → pay<0 → incasso
            st["cash"] -= pay
            st["funding"] -= pay
        st["last_funding_ts"] = now
        return st

    def account(self) -> dict:
        st = self._accrue_funding(self._state())
        self._save(st)
        unreal = sum(p["qty"] * (self.last_price(s) - p["entry"]) for s, p in st["positions"].items())
        equity = st["cash"] + unreal
        today = datetime.now(timezone.utc).date().isoformat()
        day = db.kv_get("paper_day") or {}
        if day.get("date") != today:
            day = {"date": today, "equity": equity}
            if self.writer:
                db.kv_set("paper_day", day)
        gross = sum(abs(p["qty"]) * self.last_price(s) for s, p in st["positions"].items())
        return {"equity": equity, "cash": st["cash"], "last_equity": day.get("equity", equity),
                "buying_power": max(equity * self.max_lev - gross, 0.0)}

    def positions(self) -> dict:
        st = self._state()
        out = {}
        for s, p in st["positions"].items():
            px = self.last_price(s)
            out[norm(s)] = {"qty": p["qty"], "avg_entry": p["entry"], "price": px,
                            "market_value": abs(p["qty"]) * px, "unrealized_pl": p["qty"] * (px - p["entry"]),
                            "crypto": True, "side": "short" if p["qty"] < 0 else "long"}
        return out

    def costs(self) -> dict:
        st = self._state()
        return {"fees": st.get("fees", 0.0), "funding": st.get("funding", 0.0), "start_usd": self.start_usd}

    def market_open(self) -> bool:
        return True                                    # crypto: 24/7

    def can_short(self, sym: str) -> bool:
        return True                                    # i perpetui si possono vendere allo scoperto

    # ── ordini simulati ───────────────────────────────────────────
    def _trade(self, sym: str, dq: float) -> float:
        if not self.writer:
            raise RuntimeError("solo il bot Trader può operare sul conto simulato")
        st = self._accrue_funding(self._state())
        mkt = self.last_price(sym)
        px = mkt * (1 + self.slip) if dq > 0 else mkt * (1 - self.slip)
        pos = st["positions"].get(sym)
        old = pos["qty"] if pos else 0.0
        new = old + dq
        # niente leva: il nozionale lordo dopo l'ordine non può superare il capitale
        if abs(new) > abs(old):
            unreal = sum(p["qty"] * (self.last_price(s) - p["entry"]) for s, p in st["positions"].items())
            equity = st["cash"] + unreal
            gross = sum(abs(p["qty"]) * self.last_price(s) for s, p in st["positions"].items() if s != sym)
            if gross + abs(new) * px > equity * self.max_lev + 1e-9:
                raise ValueError(f"ordine rifiutato: supererebbe la leva massima {self.max_lev:g}x")
        fee = abs(dq) * px * self.fee
        st["cash"] -= fee
        st["fees"] += fee
        if old and (old > 0) != (dq > 0):              # riduzione/chiusura: realizza il P&L
            closed = min(abs(dq), abs(old))
            st["cash"] += closed * (px - pos["entry"]) * (1 if old > 0 else -1)
        if abs(new) < 1e-12:
            st["positions"].pop(sym, None)
        elif not old or (old > 0) == (new > 0) and abs(new) > abs(old):
            entry = ((abs(old) * pos["entry"] if old else 0) + abs(dq) * px) / abs(new)   # prezzo medio
            st["positions"][sym] = {"qty": new, "entry": entry, "opened": (pos or {}).get("opened", db.now_iso())}
        elif (old > 0) != (new > 0):                   # inversione di segno: nuova posizione al prezzo attuale
            st["positions"][sym] = {"qty": new, "entry": px, "opened": db.now_iso()}
        else:
            st["positions"][sym] = {**pos, "qty": new}
        self._save(st)
        log.info("[paper] %s %+g %s @ %.6g (commissione %.4f $)", "BUY" if dq > 0 else "SELL", dq, sym, px, fee)
        return px

    def buy(self, sym: str, qty: float) -> float:
        return self._trade(sym, abs(qty))

    def sell_short(self, sym: str, qty: float) -> float:
        return self._trade(sym, -abs(qty))

    def close(self, sym: str) -> float:
        pos = self._state()["positions"].get(sym)
        if not pos:
            return self.last_price(sym)
        return self._trade(sym, -pos["qty"])

    # nessuno stop lato broker nel conto simulato: gli stop sono gestiti dal bot ogni 5 minuti
    def place_backup_stop(self, *a, **k):
        return None

    def move_backup_stop(self, order_id, *a, **k):
        return order_id

    def order_filled(self, order_id):
        return None

    def cancel_open_orders(self, sym: str):
        pass
