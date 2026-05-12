"""
providers — switchable market data layer for the TradingAgents web app.

Public API
----------
get_market_provider()   → DataProvider   (Tab 1: live price, OHLCV, indicators, news)
get_pipeline_provider() → DataProvider   (Tab 2/3: TradingAgents pipeline data)
ProviderFactory.create(name) → DataProvider

Provider names (set via .env):
  yfinance  — default, all asset classes, no API key required
  polygon   — intraday-focused, metals/forex/crypto/stocks, requires POLYGON_API_KEY
  oanda     — forex/metals real-time (not yet implemented)
  binance   — crypto (not yet implemented)

Shared types
------------
DataProvider, Capability, AssetClass, OHLCVInterval, ProviderError
"""

from .base import (
    Capability,
    AssetClass,
    OHLCVInterval,
    DataProvider,
    ProviderError,
)
from .factory import (
    ProviderFactory,
    get_market_provider,
    get_pipeline_provider,
    reset_provider_cache,
)

__all__ = [
    # Types
    "Capability",
    "AssetClass",
    "OHLCVInterval",
    "DataProvider",
    "ProviderError",
    # Factory
    "ProviderFactory",
    "get_market_provider",
    "get_pipeline_provider",
    "reset_provider_cache",
]
