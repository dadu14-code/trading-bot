"""Collegamento al broker (Alpaca: conto demo 'paper' o reale) + dati di mercato.

Per cambiare broker basta implementare la stessa interfaccia (account, positions,
market_open, bars, buy, close).
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest, CryptoLatestTradeRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from . import db
from .config import ENV

log = logging.getLogger(__name__)

TF = {"1Day": TimeFrame(1, TimeFrameUnit.Day), "4Hour": TimeFrame(4, TimeFrameUnit.Hour),
      "1Hour": TimeFrame(1, TimeFrameUnit.Hour)}
# giorni di storia da scaricare per timeframe (crypto / azioni)
LOOKBACK = {"1Day": (500, 700), "4Hour": (150, 300), "1Hour": (75, 200)}


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


def norm(symbol: str) -> str:
    return symbol.replace("/", "")


class AlpacaBroker:
    def __init__(self):
        if not ENV.alpaca_key:
            raise RuntimeError("ALPACA_API_KEY mancante nel file .env")
        self.trading = TradingClient(ENV.alpaca_key, ENV.alpaca_secret, paper=ENV.alpaca_paper)
        self.crypto_data = CryptoHistoricalDataClient(ENV.alpaca_key, ENV.alpaca_secret)
        self.stock_data = StockHistoricalDataClient(ENV.alpaca_key, ENV.alpaca_secret)
        self._cache: dict = {}

    # ── conto ─────────────────────────────────────────────────────
    def account(self) -> dict:
        a = self.trading.get_account()
        return {"equity": float(a.equity), "cash": float(a.cash), "last_equity": float(a.last_equity),
                "buying_power": float(a.buying_power)}

    def positions(self) -> dict:
        if ENV.dry_run:
            out = {}
            for t in db.open_trades():
                px = self.last_price(t["symbol"])
                out[norm(t["symbol"])] = {"qty": t["qty"], "avg_entry": t["entry_price"], "price": px,
                                          "market_value": t["qty"] * px,
                                          "unrealized_pl": (px - t["entry_price"]) * t["qty"],
                                          "crypto": is_crypto(t["symbol"])}
            return out
        return {p.symbol: {"qty": float(p.qty), "avg_entry": float(p.avg_entry_price),
                           "price": float(p.current_price), "market_value": float(p.market_value),
                           "unrealized_pl": float(p.unrealized_pl), "crypto": p.asset_class.value == "crypto"}
                for p in self.trading.get_all_positions()}

    def market_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    # ── dati ──────────────────────────────────────────────────────
    def bars(self, symbol: str, tf: str) -> pd.DataFrame:
        key = (symbol, tf)
        ttl = {"1Day": 3600, "4Hour": 900, "1Hour": 240}[tf]
        if key in self._cache and time.time() - self._cache[key][0] < ttl:
            return self._cache[key][1]
        days = LOOKBACK[tf][0 if is_crypto(symbol) else 1]
        start = datetime.now(timezone.utc) - timedelta(days=days)
        if is_crypto(symbol):
            df = self.crypto_data.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TF[tf], start=start)).df
        else:
            df = self.stock_data.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TF[tf], start=start,
                                                                 feed=DataFeed.IEX)).df
        df = df.xs(symbol, level=0) if isinstance(df.index, pd.MultiIndex) else df
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        self._cache[key] = (time.time(), df)
        return df

    def last_price(self, symbol: str) -> float:
        if is_crypto(symbol):
            r = self.crypto_data.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=symbol))
        else:
            r = self.stock_data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        return float(r[symbol].price)

    # ── ordini ────────────────────────────────────────────────────
    def buy(self, symbol: str, qty: float) -> float:
        """Acquisto a mercato. Ritorna il prezzo medio eseguito."""
        if ENV.dry_run:
            return self.last_price(symbol)
        tif = TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY
        o = self.trading.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=tif))
        return self._wait_fill(o.id, symbol)

    def close(self, symbol: str) -> float:
        if ENV.dry_run:
            return self.last_price(symbol)
        o = self.trading.close_position(norm(symbol))
        return self._wait_fill(o.id, symbol)

    def _wait_fill(self, order_id, symbol: str, timeout: int = 30) -> float:
        for _ in range(timeout):
            o = self.trading.get_order_by_id(order_id)
            if o.filled_avg_price:
                return float(o.filled_avg_price)
            time.sleep(1)
        log.warning("ordine %s non eseguito entro %ss, uso ultimo prezzo", order_id, timeout)
        return self.last_price(symbol)
