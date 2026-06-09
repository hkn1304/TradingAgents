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
    ticket:           int
    symbol:           str
    canonical_ticker: str   # app-side ticker (e.g. XAUUSD even when MT5 uses GOLD)
    direction:        str   # 'buy' | 'sell'
    volume:           float
    entry_price:      float
    current_price:    float
    sl:               float
    tp:               float
    profit:           float
    comment:          str
    open_time:        str


@dataclass
class OrderResult:
    success: bool
    ticket:  Optional[int]
    retcode: int
    comment: str


def _best_filling_mode(symbol_info):
    """Return the best supported filling mode for a symbol.

    MT5 symbol_info.filling_mode is a bitmask:
      1 = FOK (Fill or Kill)
      2 = IOC (Immediate or Cancel)
    RETURN (2 in Python API enum) works for ECN/STP where partial fills are ok.
    """
    if mt5 is None:
        return 2  # fallback
    fm = getattr(symbol_info, 'filling_mode', 0)
    if fm & 2:   # IOC supported
        return mt5.ORDER_FILLING_IOC
    if fm & 1:   # FOK supported
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN  # ECN/STP brokers


# ── XM Global symbol name map ─────────────────────────────────────────────────
# Only instruments where XM uses a completely different name (not just a suffix).
# Stocks are handled automatically by _resolve_xm_symbol() which probes MT5.
_XM_SYMBOL_MAP: dict[str, str] = {
    "XAUUSD": "GOLD",
    "XAGUSD": "SILVER",
    "XPTUSD": "PLATINUM",
    "XPDUSD": "PALLADIUM",
    "USOIL":  "OIL",
    "UKOIL":  "BRENT",
}

# Cache resolved symbols so MT5 is only probed once per ticker per session
_symbol_cache: dict[str, str] = {}

# Reverse map: MT5 symbol → canonical ticker
_XM_REVERSE_MAP: dict[str, str] = {v: k for k, v in _XM_SYMBOL_MAP.items()}

def _canonical_ticker(mt5_symbol: str) -> str:
    """Translate an XM MT5 symbol back to the app's canonical ticker name."""
    s = mt5_symbol.upper()
    if s in _XM_REVERSE_MAP:
        return _XM_REVERSE_MAP[s]
    # Check session cache (reverse lookup for auto-detected stock suffixes)
    for canonical, resolved in _symbol_cache.items():
        if resolved.upper() == s:
            return canonical
    # Strip common XM stock suffixes (.N, .NAS, etc.)
    for suffix in ('.NAS', '.NYSE', '.N', '.US'):
        if s.endswith(suffix):
            return s[:-len(suffix)]
    return s

def _xm_symbol(ticker: str) -> str:
    """
    Translate a canonical ticker to the XM MT5 symbol name.
    1. Check explicit map (metals, energy).
    2. Check session cache.
    3. Probe MT5 for ticker, ticker.N, ticker.NAS in that order.
    4. Fall back to the raw ticker (MT5 will return a clear error).
    """
    t = ticker.upper()
    if t in _XM_SYMBOL_MAP:
        return _XM_SYMBOL_MAP[t]
    if t in _symbol_cache:
        return _symbol_cache[t]
    if _MT5_AVAILABLE and mt5.terminal_info() is not None:
        for candidate in [t, t + '.N', t + '.NAS', t + '.NYSE', t + '.US']:
            if mt5.symbol_info(candidate) is not None:
                _symbol_cache[t] = candidate
                logger.info(f"Resolved XM symbol: {t} → {candidate}")
                return candidate
    return t  # fallback — MT5 will return a descriptive error


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

    # Known terminal paths — tried in order when no running terminal is found
    _TERMINAL_PATHS = [
        r"C:\Program Files\XM Global MT5\terminal64.exe",
        r"C:\Program Files\ALB Yatirim MetaTrader 5 Terminal\terminal64.exe",
        r"C:\Program Files\GCM MT5 Terminal\terminal64.exe",
        r"C:\Program Files (x86)\GCM MetaTrader\terminal64.exe",
    ]

    def connect(self, login: int, password: str, server: str) -> tuple[bool, str]:
        if not _MT5_AVAILABLE:
            return False, "MetaTrader5 package not installed (pip install MetaTrader5)"

        import os

        # First try: attach to any already-running terminal (no path)
        ok = mt5.initialize(login=login, password=password, server=server, timeout=10000)
        if ok:
            self._connected = True
            self._login  = login
            self._server = server
            logger.info(f"MT5 connected (running terminal): login={login} server={server}")
            return True, "Connected"

        # Second try: launch each known terminal in API mode
        for path in self._TERMINAL_PATHS:
            if not os.path.exists(path):
                continue
            logger.info(f"Trying terminal: {path}")
            ok = mt5.initialize(path=path, login=login, password=password,
                                server=server, timeout=30000)
            if ok:
                self._connected = True
                self._login  = login
                self._server = server
                logger.info(f"MT5 connected via {path}: login={login} server={server}")
                return True, "Connected"
            logger.warning(f"Terminal {path} failed: {mt5.last_error()}")

        last_err = mt5.last_error()
        return False, (
            f"MT5 connect failed: {last_err}. "
            "Make sure MetaTrader 5 is installed and 'Algo Trading' is enabled "
            "in Tools → Options → Expert Advisors."
        )

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
                ticket           = p.ticket,
                symbol           = p.symbol,
                canonical_ticker = _canonical_ticker(p.symbol),
                direction        = 'buy' if p.type == mt5.POSITION_TYPE_BUY else 'sell',
                volume           = p.volume,
                entry_price      = p.price_open,
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
        mt5_sym = _xm_symbol(symbol)
        hits = [p for p in self.get_positions() if p.symbol == mt5_sym]
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

        symbol = _xm_symbol(symbol)  # translate XAUUSD→GOLD etc.

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

        # Pick filling mode supported by this symbol
        filling_mode = _best_filling_mode(info)

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
            "type_filling": filling_mode,
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
        close_info = mt5.symbol_info(pos.symbol)
        filling    = _best_filling_mode(close_info) if close_info else mt5.ORDER_FILLING_RETURN

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
            "type_filling": filling,
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
        info = mt5.symbol_info(_xm_symbol(symbol))
        if info is None:
            return round(max(0.01, volume), 2)
        vol  = max(info.volume_min, min(info.volume_max, volume))
        step = info.volume_step
        return round(round(vol / step) * step, 8)
