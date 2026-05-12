"""Binance provider stub — crypto spot and futures."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .base import Capability, DataProvider, OHLCVInterval, ProviderError


class BinanceProvider(DataProvider):
    """
    Binance REST API provider.

    Best for: crypto spot and futures, real-time order book and price data.
    Requires: BINANCE_API_KEY and BINANCE_API_SECRET in .env
              pip install python-binance

    Not yet implemented — contributions welcome.
    """

    CAPABILITIES = {
        Capability.LIVE_PRICE,
        Capability.OHLCV_DAILY,
        Capability.OHLCV_INTRADAY,
        Capability.CRYPTO,
    }

    def __init__(self):
        raise ProviderError(
            "BinanceProvider is not yet implemented. "
            "Set MARKET_DATA_PROVIDER=yfinance in your .env to use the default provider."
        )

    def normalize_ticker(self, ticker: str) -> str:
        raise NotImplementedError

    def get_price_live(self, ticker: str) -> Optional[float]:
        raise NotImplementedError

    def get_ohlcv(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: OHLCVInterval = OHLCVInterval.DAY_1,
    ) -> pd.DataFrame:
        raise NotImplementedError

    def get_indicators(self, ticker: str, date: str) -> dict:
        raise NotImplementedError

    def get_news(self, ticker: str, max_items: int = 6) -> list[dict]:
        raise NotImplementedError

    def get_fundamentals(self, ticker: str, date: str) -> dict:
        raise NotImplementedError
