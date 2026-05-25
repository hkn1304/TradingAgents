"""Oanda provider stub — forex and metals, real-time streaming capable."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .base import Capability, DataProvider, OHLCVInterval, ProviderError


class OandaProvider(DataProvider):
    """
    Oanda REST API provider.

    Best for: live forex and metals spot pricing, high-quality intraday bars.
    Requires: OANDA_API_KEY and OANDA_ACCOUNT_ID in .env
              pip install oandapyV20

    Not yet implemented — contributions welcome.
    """

    CAPABILITIES = {
        Capability.LIVE_PRICE,
        Capability.OHLCV_DAILY,
        Capability.OHLCV_INTRADAY,
        Capability.FOREX,
        Capability.METALS,
    }

    def __init__(self):
        raise ProviderError(
            "OandaProvider is not yet implemented. "
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
