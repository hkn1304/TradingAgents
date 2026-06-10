"""
Symbol translation between data-provider tickers (Yahoo-style) and
MT5 broker symbols.

Brokers name metals/FX/CFDs inconsistently: Yahoo's XAGUSD is "SILVER"
on some MT5 servers, "XAGUSD" on others, "XAGUSD.r" or "Silver_oz"
elsewhere.  This module keeps a default candidate table for common
instruments and falls back to a live search of the broker's symbol
tree when none of the candidates exist.

Resolved mappings are cached per process so the search runs once.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# data ticker → broker symbol candidates, tried in order
DATA_TO_MT5_CANDIDATES: dict[str, list[str]] = {
    "XAGUSD": ["XAGUSD", "SILVER", "Silver", "XAGUSD.r", "XAGUSDm", "SILVERoz"],
    "XAUUSD": ["XAUUSD", "GOLD", "Gold", "XAUUSD.r", "XAUUSDm", "GOLDoz"],
    "XPTUSD": ["XPTUSD", "PLATINUM", "Platinum"],
    "XPDUSD": ["XPDUSD", "PALLADIUM", "Palladium"],
    "BTCUSD": ["BTCUSD", "Bitcoin", "BTCUSD.r", "BTCUSDm"],
    "ETHUSD": ["ETHUSD", "Ethereum", "ETHUSDm"],
    "USOIL":  ["USOIL", "WTI", "CrudeOIL", "XTIUSD", "OIL"],
    "UKOIL":  ["UKOIL", "BRENT", "XBRUSD"],
}

# broker symbol → data ticker (reverse lookup for the guardian)
MT5_TO_DATA: dict[str, str] = {
    "SILVER":    "XAGUSD",
    "SILVEROZ":  "XAGUSD",
    "GOLD":      "XAUUSD",
    "GOLDOZ":    "XAUUSD",
    "PLATINUM":  "XPTUSD",
    "PALLADIUM": "XPDUSD",
    "BITCOIN":   "BTCUSD",
    "ETHEREUM":  "ETHUSD",
    "WTI":       "USOIL",
    "CRUDEOIL":  "USOIL",
    "XTIUSD":    "USOIL",
    "BRENT":     "UKOIL",
    "XBRUSD":    "UKOIL",
}

# data-ticker aliases so users can type the broker name in the Portfolio tab
DATA_ALIASES: dict[str, str] = {
    "SILVER": "XAGUSD",
    "GOLD":   "XAUUSD",
}

_resolved_cache: dict[str, str] = {}   # data ticker → confirmed broker symbol


def clear_symbol_cache():
    """Drop resolved mappings — must be called when the broker account
    changes, since symbol names differ between brokers (XM: SILVER,
    MetaQuotes demo: XAGUSD, …)."""
    _resolved_cache.clear()


def normalize_data_ticker(ticker: str) -> str:
    """Map broker-style names typed by the user to data-provider tickers."""
    t = ticker.upper().strip()
    return DATA_ALIASES.get(t, t)


def to_data_symbol(mt5_symbol: str) -> str:
    """Broker symbol → data ticker (identity when unknown)."""
    upper = mt5_symbol.upper()
    base  = upper.split('.')[0]  # strip dotted broker suffix: SILVER.r → SILVER
    return MT5_TO_DATA.get(upper, MT5_TO_DATA.get(base, mt5_symbol))


def resolve_mt5_symbol(broker, ticker: str) -> str:
    """
    Find the broker's actual symbol for a data ticker.

    Order: cache → exact ticker → candidate table → live substring
    search via broker.find_symbol().  Returns the input unchanged if
    nothing matches (the order will then fail with a clear message).
    """
    t = normalize_data_ticker(ticker)
    if not getattr(broker, "connected", False):
        return _resolved_cache.get(t, t)

    # Validate cache against the live broker — a cached name from a
    # previous account (demo vs real, different broker) may not exist here
    if t in _resolved_cache:
        cached = _resolved_cache[t]
        if broker.symbol_exists(cached):
            return cached
        del _resolved_cache[t]

    candidates = DATA_TO_MT5_CANDIDATES.get(t, [t])
    if t not in candidates:
        candidates = [t] + candidates
    for cand in candidates:
        if broker.symbol_exists(cand):
            _resolved_cache[t] = cand
            if cand != t:
                logger.info(f"Symbol mapped: {t} → {cand}")
            return cand

    # Last resort: search the broker's symbol tree
    found = broker.find_symbol(t)
    if found:
        _resolved_cache[t] = found
        logger.info(f"Symbol discovered: {t} → {found}")
        return found

    logger.warning(f"No MT5 symbol found for {t}")
    return t
