"""
MetaTrader 5 broker adapter.

Wraps the official MetaTrader5 Python package with a clean interface.
Handles connect/disconnect, order execution, position management,
and account info queries.

Requirements (Windows):
    pip install MetaTrader5

The MT5 terminal must be running with "Algo Trading" enabled in
Tools → Options → Expert Advisors before calling connect().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore
    _MT5_AVAILABLE = False
    logger.warning("MetaTrader5 package not installed — MT5 features disabled. "
                   "Run: pip install MetaTrader5")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class AccountInfo:
    login:       int
    server:      str
    balance:     float
    equity:      float
    margin:      float
    margin_free: float
    leverage:    int
    currency:    str
    profit:      float


@dataclass
class Position:
    ticket:        int
    symbol:        str
    direction:     str    # 'buy' | 'sell'
    volume:        float
    entry_price:   float
    current_price: float
    sl:            float
    tp:            float
    profit:        float
    comment:       str
    open_time:     str


@dataclass
class OrderResult:
    success: bool
    ticket:  Optional[int]
    retcode: int
    comment: str


# ── Broker ────────────────────────────────────────────────────────────────────

class MT5Broker:
    """
    Thread-safe wrapper around the MetaTrader5 Python package.
    Intended as an application-level singleton (created once in server.py).
    """

    def __init__(self):
        self._connected = False
        self._login:  Optional[int] = None
        self._server: Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self, login: int, password: str, server: str) -> tuple[bool, str]:
        if not _MT5_AVAILABLE:
            return False, "MetaTrader5 package not installed (pip install MetaTrader5)"

        if not mt5.initialize():
            return False, f"MT5 initialize failed: {mt5.last_error()}"

        if not mt5.login(login, password=password, server=server):
            mt5.shutdown()
            return False, f"Login failed: {mt5.last_error()}"

        self._connected = True
        self._login  = login
        self._server = server
        logger.info(f"MT5 connected: login={login} server={server}")
        return True, "Connected"

    def disconnect(self):
        if _MT5_AVAILABLE and self._connected:
            mt5.shutdown()
        self._connected = False
        logger.info("MT5 disconnected")

    @property
    def connected(self) -> bool:
        return self._connected and _MT5_AVAILABLE

    # ── Account ───────────────────────────────────────────────────────────────

    def get_account_info(self) -> Optional[AccountInfo]:
        if not self.connected:
            return None
        info = mt5.account_info()
        if info is None:
            return None
        return AccountInfo(
            login       = info.login,
            server      = info.server,
            balance     = round(info.balance,     2),
            equity      = round(info.equity,      2),
            margin      = round(info.margin,      2),
            margin_free = round(info.margin_free, 2),
            leverage    = info.leverage,
            currency    = info.currency,
            profit      = round(info.profit,      2),
        )

    # ── Positions ─────────────────────────────────────────────────────────────

    def get_positions(self) -> list[Position]:
        if not self.connected:
            return []
        raw = mt5.positions_get()
        if not raw:
            return []
        return [
            Position(
                ticket        = p.ticket,
                symbol        = p.symbol,
                direction     = 'buy' if p.type == mt5.POSITION_TYPE_BUY else 'sell',
                volume        = p.volume,
                entry_price   = p.price_open,
                current_price = p.price_current,
                sl            = p.sl,
                tp            = p.tp,
                profit        = round(p.profit, 2),
                comment       = p.comment,
                open_time     = str(p.time),
            )
            for p in raw
        ]

    def get_position_for_symbol(self, symbol: str) -> Optional[Position]:
        hits = [p for p in self.get_positions() if p.symbol == symbol]
        return hits[0] if hits else None

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_market_order(
        self,
        symbol:    str,
        direction: str,              # 'buy' | 'sell'
        volume:    float,
        sl:        Optional[float] = None,
        tp:        Optional[float] = None,
        comment:   str  = "TradingAgents",
        magic:     int  = 20250101,
    ) -> OrderResult:
        if not self.connected:
            return OrderResult(False, None, -1, "Not connected")

        info = mt5.symbol_info(symbol)
        if info is None:
            return OrderResult(False, None, -1, f"Symbol '{symbol}' not found in MT5")
        if not info.visible:
            mt5.symbol_select(symbol, True)

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return OrderResult(False, None, -1, "Cannot get live tick for symbol")

        order_type = mt5.ORDER_TYPE_BUY  if direction == 'buy'  else mt5.ORDER_TYPE_SELL
        price      = tick.ask            if direction == 'buy'  else tick.bid

        request: dict = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       float(volume),
            "type":         order_type,
            "price":        price,
            "deviation":    20,
            "magic":        magic,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if sl is not None: request["sl"] = sl
        if tp is not None: request["tp"] = tp

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(False, None, -1, f"order_send returned None: {mt5.last_error()}")

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Order placed: {direction} {volume} {symbol} ticket={result.order}")
            return OrderResult(True, result.order, result.retcode, result.comment)

        logger.warning(f"Order failed: retcode={result.retcode} — {result.comment}")
        return OrderResult(False, None, result.retcode, result.comment)

    def close_position(self, ticket: int) -> OrderResult:
        if not self.connected:
            return OrderResult(False, None, -1, "Not connected")

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(False, None, -1, f"Position {ticket} not found")

        pos  = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            return OrderResult(False, None, -1, "Cannot get tick price for close")

        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price      = tick.bid            if pos.type == mt5.POSITION_TYPE_BUY else tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        price,
            "deviation":    20,
            "magic":        pos.magic,
            "comment":      "close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return OrderResult(False, None, -1, str(mt5.last_error()))

        success = result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(success, result.order if success else None, result.retcode, result.comment)

    # ── Symbol helpers ────────────────────────────────────────────────────────

    def normalize_volume(self, symbol: str, volume: float) -> float:
        """Round volume to the symbol's lot step, clamped to [min, max]."""
        if not self.connected:
            return round(max(0.01, volume), 2)
        info = mt5.symbol_info(symbol)
        if info is None:
            return round(max(0.01, volume), 2)
        vol  = max(info.volume_min, min(info.volume_max, volume))
        step = info.volume_step
        return round(round(vol / step) * step, 8)
