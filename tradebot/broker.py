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
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, MarketOrderRequest, ReplaceOrderRequest,
                                    StopLimitOrderRequest, StopOrderRequest)

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
                sgn = -1 if t.get("side") == "short" else 1
                out[norm(t["symbol"])] = {"qty": sgn * t["qty"], "avg_entry": t["entry_price"], "price": px,
                                          "market_value": t["qty"] * px,
                                          "unrealized_pl": sgn * (px - t["entry_price"]) * t["qty"],
                                          "crypto": is_crypto(t["symbol"]), "side": "short" if sgn < 0 else "long"}
            return out
        # market_value sempre positivo (esposizione lorda); gli short hanno qty negativa
        return {p.symbol: {"qty": float(p.qty), "avg_entry": float(p.avg_entry_price),
                           "price": float(p.current_price), "market_value": abs(float(p.market_value)),
                           "unrealized_pl": float(p.unrealized_pl), "crypto": p.asset_class.value == "crypto",
                           "side": "short" if float(p.qty) < 0 else "long"}
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

    def can_short(self, symbol: str) -> bool:
        if ENV.dry_run:
            return True
        try:
            a = self.trading.get_asset(symbol)
            return bool(a.shortable and a.easy_to_borrow)
        except Exception as e:
            log.warning("verifica shortabilità %s: %s", symbol, e)
            return False

    def sell_short(self, symbol: str, qty: int) -> float:
        """Vendita allo scoperto a mercato (solo azioni/ETF, quantità intere)."""
        if ENV.dry_run:
            return self.last_price(symbol)
        o = self.trading.submit_order(MarketOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.SELL,
                                                         time_in_force=TimeInForce.DAY))
        return self._wait_fill(o.id, symbol)

    def close(self, symbol: str) -> float:
        if ENV.dry_run:
            return self.last_price(symbol)
        self.cancel_open_orders(symbol)  # libera la quantità bloccata dallo stop di emergenza
        o = self.trading.close_position(norm(symbol))
        return self._wait_fill(o.id, symbol)

    # ── stop di emergenza lato broker ─────────────────────────────
    def place_backup_stop(self, symbol: str, qty: float, stop: float, side: str = "long") -> str | None:
        """Ordine di stop che vive sul server del broker: scatta anche se il bot è spento.
        Long → stop di vendita sotto il prezzo; short → stop di acquisto sopra il prezzo."""
        if ENV.dry_run:
            return None
        try:
            if side == "short":
                req = StopOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                                       stop_price=round(stop, 2))
            elif is_crypto(symbol):  # le crypto su Alpaca accettano stop-limit, non stop a mercato
                req = StopLimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                                            stop_price=round(stop, 2), limit_price=round(stop * 0.97, 2))
            else:
                req = StopOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                                       stop_price=round(stop, 2))
            return str(self.trading.submit_order(req).id)
        except Exception as e:
            log.warning("stop di emergenza %s non piazzato: %s", symbol, e)
            return None

    def move_backup_stop(self, order_id: str, symbol: str, stop: float) -> str | None:
        if ENV.dry_run or not order_id:
            return order_id
        try:
            lim = round(stop * 0.97, 2) if is_crypto(symbol) else None
            return str(self.trading.replace_order_by_id(order_id, ReplaceOrderRequest(stop_price=round(stop, 2), limit_price=lim)).id)
        except Exception as e:
            log.warning("spostamento stop di emergenza %s fallito: %s", symbol, e)
            return order_id

    def order_filled(self, order_id: str | None) -> float | None:
        if ENV.dry_run or not order_id:
            return None
        try:
            o = self.trading.get_order_by_id(order_id)
            return float(o.filled_avg_price) if o.filled_avg_price else None
        except Exception:
            return None

    def cancel_open_orders(self, symbol: str):
        try:
            for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
                self.trading.cancel_order_by_id(o.id)
        except Exception as e:
            log.warning("cancellazione ordini %s: %s", symbol, e)

    def _wait_fill(self, order_id, symbol: str, timeout: int = 30) -> float:
        for _ in range(timeout):
            o = self.trading.get_order_by_id(order_id)
            if o.filled_avg_price:
                return float(o.filled_avg_price)
            time.sleep(1)
        log.warning("ordine %s non eseguito entro %ss, uso ultimo prezzo", order_id, timeout)
        return self.last_price(symbol)


def get_broker(cfg: dict | None = None, writer: bool = False):
    """Broker scelto in settings.yaml: 'alpaca' (demo/reale Alpaca) o 'kraken_paper'
    (conto simulato sui prezzi veri di Kraken Futures)."""
    from .config import load_settings
    cfg = cfg or load_settings()
    name = cfg.get("broker", "alpaca")
    if name == "kraken_paper":
        from .paper import PaperBroker
        return PaperBroker(cfg, writer=writer)
    if name == "alpaca":
        return AlpacaBroker()
    raise ValueError(f"broker sconosciuto: {name}")
