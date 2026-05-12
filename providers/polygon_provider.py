"""Polygon.io data provider — strong intraday coverage, metals/forex/stocks/crypto."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests

from .base import Capability, DataProvider, OHLCVInterval, ProviderError

logger = logging.getLogger(__name__)

# ── Canonical → Polygon ticker translation ─────────────────────────────────────

_TICKER_MAP: dict[str, str] = {
    # Metals  (Forex/CFD)
    "XAUUSD": "C:XAUUSD",
    "XAGUSD": "C:XAGUSD",
    "XPTUSD": "C:XPTUSD",
    "XPDUSD": "C:XPDUSD",
    # Major forex
    "EURUSD": "C:EURUSD",
    "GBPUSD": "C:GBPUSD",
    "USDJPY": "C:USDJPY",
    "AUDUSD": "C:AUDUSD",
    "USDCAD": "C:USDCAD",
    "USDCHF": "C:USDCHF",
    "NZDUSD": "C:NZDUSD",
    # Crypto
    "BTCUSD":  "X:BTCUSD",
    "ETHUSD":  "X:ETHUSD",
    "BNBUSD":  "X:BNBUSD",
    "SOLUSD":  "X:SOLUSD",
    "XRPUSD":  "X:XRPUSD",
    "DOGEUSD": "X:DOGEUSD",
    # Stocks have no prefix — passed through as-is (e.g. "AAPL")
}

# Polygon timespan strings
_TIMESPAN_MAP: dict[OHLCVInterval, tuple[str, int]] = {
    OHLCVInterval.MIN_1:  ("minute", 1),
    OHLCVInterval.MIN_5:  ("minute", 5),
    OHLCVInterval.MIN_15: ("minute", 15),
    OHLCVInterval.MIN_30: ("minute", 30),
    OHLCVInterval.HOUR_1: ("hour",   1),
    OHLCVInterval.HOUR_4: ("hour",   4),
    OHLCVInterval.DAY_1:  ("day",    1),
    OHLCVInterval.WEEK_1: ("week",   1),
}

_PRICE_TTL = 10
_price_cache: dict[str, dict] = {}


class PolygonProvider(DataProvider):
    """
    Polygon.io provider.

    Requires POLYGON_API_KEY in the environment.
    Best suited for intraday bars on metals, forex, crypto, and US stocks.
    Does NOT support fundamentals or news (use yfinance for those).
    """

    CAPABILITIES = {
        Capability.LIVE_PRICE,
        Capability.OHLCV_DAILY,
        Capability.OHLCV_INTRADAY,
        Capability.STOCKS,
        Capability.CRYPTO,
        Capability.FOREX,
        Capability.METALS,
    }

    def __init__(self):
        self._api_key = os.getenv("POLYGON_API_KEY", "")
        if not self._api_key:
            raise ProviderError(
                "POLYGON_API_KEY is not set. Add it to your .env file."
            )

    # ── Ticker normalization ──────────────────────────────────────────────────

    def normalize_ticker(self, ticker: str) -> str:
        return _TICKER_MAP.get(ticker.upper(), ticker.upper())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get(self, url: str, params: dict) -> dict:
        params["apiKey"] = self._api_key
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _aggs(
        self,
        poly_ticker: str,
        timespan: str,
        multiplier: int,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{poly_ticker}"
            f"/range/{multiplier}/{timespan}/{start_date}/{end_date}"
        )
        data = self._get(url, {"sort": "asc", "limit": 50000})
        results = data.get("results", [])
        if not results:
            return pd.DataFrame()

        df = pd.DataFrame(results)
        df["Date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
        df = df.rename(columns={
            "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume",
        })
        return df[["Date", "Open", "High", "Low", "Close", "Volume"]].sort_values("Date").reset_index(drop=True)

    # ── Live price ────────────────────────────────────────────────────────────

    def get_price_live(self, ticker: str) -> Optional[float]:
        poly_ticker = self.normalize_ticker(ticker)
        cached = _price_cache.get(poly_ticker)
        if cached and (time.time() - cached["ts"]) < _PRICE_TTL:
            return cached["price"]
        try:
            url  = f"https://api.polygon.io/v2/last/trade/{poly_ticker}"
            data = self._get(url, {})
            price = data.get("results", {}).get("p")
            if price:
                result = round(float(price), 4)
                _price_cache[poly_ticker] = {"price": result, "ts": time.time()}
                return result
        except Exception as exc:
            logger.warning("Polygon live price failed for %s: %s", poly_ticker, exc)
        return _price_cache.get(poly_ticker, {}).get("price")

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    def get_ohlcv(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: OHLCVInterval = OHLCVInterval.DAY_1,
    ) -> pd.DataFrame:
        poly_ticker = self.normalize_ticker(ticker)
        timespan, multiplier = _TIMESPAN_MAP[interval]

        # Polygon 4H: try native first, fall back to 1H resample
        if interval == OHLCVInterval.HOUR_4:
            try:
                df = self._aggs(poly_ticker, "hour", 4, start_date, end_date)
                if len(df) >= 10:
                    return df
            except Exception:
                pass
            # Resample from 1H
            df1h = self._aggs(poly_ticker, "hour", 1, start_date, end_date)
            if df1h.empty:
                return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
            df1h = df1h.set_index("Date")
            df = df1h.resample("4h").agg(
                Open=("Open", "first"), High=("High", "max"),
                Low=("Low", "min"),    Close=("Close", "last"),
                Volume=("Volume", "sum"),
            ).dropna().reset_index()
            return df

        return self._aggs(poly_ticker, timespan, multiplier, start_date, end_date)

    # ── Indicators ────────────────────────────────────────────────────────────

    def get_indicators(self, ticker: str, date: str) -> dict:
        """
        Compute indicators from Polygon OHLCV.
        Fetches 300 days of daily bars so rolling windows are warm.
        """
        end   = date
        start = (pd.Timestamp(date) - pd.DateOffset(days=365)).strftime("%Y-%m-%d")
        df    = self.get_ohlcv(ticker, start, end, OHLCVInterval.DAY_1)

        if df.empty or len(df) < 50:
            return {}

        from stockstats import wrap
        stock_df = wrap(df.copy())
        row = stock_df.iloc[[-1]]

        indicators = [
            "rsi", "macd", "macds", "macdh",
            "close_50_sma", "close_200_sma", "close_10_ema",
            "boll", "boll_ub", "boll_lb", "atr",
        ]
        result: dict = {}
        for ind in indicators:
            try:
                stock_df[ind]
                val = row[ind].values[0]
                result[ind] = round(float(val), 4) if not pd.isna(val) else None
            except Exception:
                result[ind] = None

        try:
            lo14 = df["Low"].rolling(14).min()
            hi14 = df["High"].rolling(14).max()
            k = 100 * (df["Close"] - lo14) / (hi14 - lo14)
            result["stoch_k"] = round(float(k.iloc[-1]), 2)
            result["stoch_d"] = round(float(k.rolling(3).mean().iloc[-1]), 2)
        except Exception:
            result["stoch_k"] = None
            result["stoch_d"] = None

        return result

    # ── News — not supported ──────────────────────────────────────────────────

    def get_news(self, ticker: str, max_items: int = 6) -> list[dict]:
        raise ProviderError(
            "PolygonProvider does not support news. "
            "Use YFinanceProvider or pair with a news-capable provider."
        )

    # ── Fundamentals — not supported ─────────────────────────────────────────

    def get_fundamentals(self, ticker: str, date: str) -> dict:
        raise ProviderError(
            "PolygonProvider does not support fundamentals. "
            "Use YFinanceProvider for fundamental data."
        )
