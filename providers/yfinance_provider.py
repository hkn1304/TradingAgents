"""yfinance data provider — default for all asset classes."""

from __future__ import annotations

import time
import logging
import threading
from typing import Optional

import pandas as pd
import yfinance as yf
from stockstats import wrap

from .base import Capability, DataProvider, OHLCVInterval, ProviderError

# Re-use the caching + retry logic already in tradingagents
from tradingagents.dataflows.stockstats_utils import load_ohlcv

logger = logging.getLogger(__name__)

# yf.download() is not thread-safe — concurrent calls swap each other's data.
# This lock serialises all downloads so parallel API requests don't corrupt results.
_yf_download_lock = threading.Lock()

# ── Canonical → yfinance ticker translation ────────────────────────────────────

_TICKER_MAP: dict[str, str] = {
    # Metals (futures)
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "XPTUSD": "PL=F",
    "XPDUSD": "PA=F",
    # Crypto
    "BTCUSD":  "BTC-USD",
    "ETHUSD":  "ETH-USD",
    "BNBUSD":  "BNB-USD",
    "SOLUSD":  "SOL-USD",
    "XRPUSD":  "XRP-USD",
    "ADAUSD":  "ADA-USD",
    "DOGEUSD": "DOGE-USD",
    # Forex
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",
    "NZDUSD": "NZDUSD=X",
}

# yfinance interval strings for each OHLCVInterval value
# Note: 4H is not native — we resample from 1H
_INTERVAL_MAP: dict[OHLCVInterval, str] = {
    OHLCVInterval.MIN_1:  "1m",
    OHLCVInterval.MIN_5:  "5m",
    OHLCVInterval.MIN_15: "15m",
    OHLCVInterval.MIN_30: "30m",
    OHLCVInterval.HOUR_1: "1h",
    OHLCVInterval.HOUR_4: "1h",   # downloaded as 1H, resampled below
    OHLCVInterval.DAY_1:  "1d",
    OHLCVInterval.WEEK_1: "1wk",
}

# Indicators computed via stockstats in get_indicators()
_STOCKSTATS_INDICATORS = [
    "rsi",
    "macd", "macds", "macdh",
    "close_50_sma", "close_200_sma", "close_10_ema",
    "boll", "boll_ub", "boll_lb",
    "atr",
]

# Short TTL for live price cache (seconds)
_PRICE_TTL = 10
_price_cache: dict[str, dict] = {}


class YFinanceProvider(DataProvider):
    """
    Full-featured provider backed by yfinance.

    Supports stocks, ETFs, metals (via futures), crypto, and major forex pairs.
    Fundamentals are only meaningful for stocks/ETFs.
    OHLCV uses the cached loader from tradingagents.dataflows for indicator
    requests (benefits from the 5-year cache on disk) and direct downloads
    for arbitrary date-range requests.
    """

    CAPABILITIES = {
        Capability.LIVE_PRICE,
        Capability.OHLCV_DAILY,
        Capability.OHLCV_INTRADAY,
        Capability.FUNDAMENTALS,
        Capability.NEWS,
        Capability.STOCKS,
        Capability.CRYPTO,
        Capability.FOREX,
        Capability.METALS,
    }

    # ── Ticker normalization ──────────────────────────────────────────────────

    def normalize_ticker(self, ticker: str) -> str:
        """Return the yfinance-native symbol for a canonical ticker."""
        canonical = ticker.upper().lstrip("$")
        return _TICKER_MAP.get(canonical, canonical)

    # ── Live price ────────────────────────────────────────────────────────────

    def get_price_live(self, ticker: str) -> Optional[float]:
        yf_ticker = self.normalize_ticker(ticker)
        cached = _price_cache.get(yf_ticker)
        if cached and (time.time() - cached["ts"]) < _PRICE_TTL:
            return cached["price"]
        try:
            info = yf.Ticker(yf_ticker).fast_info
            price = getattr(info, "last_price", None) or getattr(info, "regular_market_price", None)
            if price and float(price) > 0:
                result = round(float(price), 4)
                _price_cache[yf_ticker] = {"price": result, "ts": time.time()}
                return result
        except Exception as exc:
            logger.warning("yfinance live price failed for %s: %s", yf_ticker, exc)
        # Return stale cache rather than nothing
        return _price_cache.get(yf_ticker, {}).get("price")

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    def get_ohlcv(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: OHLCVInterval = OHLCVInterval.DAY_1,
    ) -> pd.DataFrame:
        yf_ticker = self.normalize_ticker(ticker)
        yf_interval = _INTERVAL_MAP[interval]

        with _yf_download_lock:
            raw = yf.download(
                yf_ticker,
                start=start_date,
                end=end_date,
                interval=yf_interval,
                progress=False,
                auto_adjust=True,
                multi_level_index=False,
            )

        if raw.empty:
            return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])

        df = raw.reset_index()

        # Normalize the date column name (daily → "Date", intraday → "Datetime")
        if "Datetime" in df.columns:
            df = df.rename(columns={"Datetime": "Date"})

        # Strip timezone so all callers get naive datetimes
        if pd.api.types.is_datetime64tz_dtype(df["Date"]):
            df["Date"] = df["Date"].dt.tz_localize(None)

        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
        df = df.sort_values("Date").reset_index(drop=True)

        # 4H is not native in yfinance — resample from the 1H download
        if interval == OHLCVInterval.HOUR_4:
            df = df.set_index("Date")
            df = df.resample("4h").agg(
                Open=("Open", "first"),
                High=("High", "max"),
                Low=("Low", "min"),
                Close=("Close", "last"),
                Volume=("Volume", "sum"),
            ).dropna().reset_index()

        return df

    # ── Indicators ────────────────────────────────────────────────────────────

    def get_indicators(self, ticker: str, date: str) -> dict:
        """
        Compute all standard indicators as of `date`.

        Uses the cached OHLCV loader so repeated calls for the same ticker
        do not re-download data. All stockstats indicators are computed in
        one pass; stochastic is added manually.
        """
        yf_ticker = self.normalize_ticker(ticker)

        df = load_ohlcv(yf_ticker, date)
        if df.empty or len(df) < 50:
            return {ind: None for ind in _STOCKSTATS_INDICATORS + ["stoch_k", "stoch_d"]}

        stock_df = wrap(df.copy())

        # Trigger all stockstats computations first — they add columns to stock_df in-place.
        # Row selection must happen AFTER this loop; a slice taken before the columns exist
        # is a pandas copy and won't receive the new columns.
        for ind in _STOCKSTATS_INDICATORS:
            try:
                stock_df[ind]
            except Exception:
                pass

        # Now select the target row (after all columns exist on stock_df)
        date_str = pd.to_datetime(date).strftime("%Y-%m-%d")
        mask = stock_df["Date"].dt.strftime("%Y-%m-%d") == date_str
        row = stock_df[mask] if mask.any() else stock_df.iloc[[-1]]

        result: dict = {}
        for ind in _STOCKSTATS_INDICATORS:
            try:
                val = row[ind].values[0]
                result[ind] = round(float(val), 4) if not pd.isna(val) else None
            except Exception:
                result[ind] = None

        # Stochastic %K / %D (not in stockstats default; computed on raw df)
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

    # ── News ─────────────────────────────────────────────────────────────────

    def get_news(self, ticker: str, max_items: int = 6) -> list[dict]:
        yf_ticker = self.normalize_ticker(ticker)
        try:
            raw_news = yf.Ticker(yf_ticker).news or []
        except Exception as exc:
            logger.warning("yfinance news failed for %s: %s", yf_ticker, exc)
            return []

        items: list[dict] = []
        for n in raw_news[:max_items]:
            # yfinance ≥0.2.x nests data under a "content" dict
            content = n.get("content") or n
            title = content.get("title") or ""
            url   = (
                (content.get("canonicalUrl") or {}).get("url")
                or content.get("url")
                or n.get("link", "")
            )
            source    = (content.get("provider") or {}).get("displayName") or ""
            published = content.get("pubDate") or n.get("providerPublishTime", "")

            if title:
                items.append({
                    "title":     title,
                    "url":       url,
                    "source":    source,
                    "published": str(published),
                })

        return items

    # ── Fundamentals ──────────────────────────────────────────────────────────

    def get_fundamentals(self, ticker: str, date: str) -> dict:
        """
        Returns a flat dict of fundamental metrics from yfinance.
        Only meaningful for stocks and ETFs.
        Metals, crypto, and forex will return an empty dict.
        """
        self.require(Capability.FUNDAMENTALS)
        yf_ticker = self.normalize_ticker(ticker)

        # Skip fundamentals for non-equity tickers
        if yf_ticker.endswith(("=F", "-USD", "-USDT", "=X")):
            return {}

        try:
            info = yf.Ticker(yf_ticker).info or {}
        except Exception as exc:
            logger.warning("yfinance fundamentals failed for %s: %s", yf_ticker, exc)
            return {}

        def _get(key: str):
            v = info.get(key)
            return round(float(v), 4) if isinstance(v, (int, float)) else v

        return {
            "pe_ratio":       _get("trailingPE"),
            "forward_pe":     _get("forwardPE"),
            "market_cap":     _get("marketCap"),
            "revenue":        _get("totalRevenue"),
            "profit_margin":  _get("profitMargins"),
            "gross_margin":   _get("grossMargins"),
            "debt_to_equity": _get("debtToEquity"),
            "roe":            _get("returnOnEquity"),
            "roa":            _get("returnOnAssets"),
            "eps":            _get("trailingEps"),
            "dividend_yield": _get("dividendYield"),
            "beta":           _get("beta"),
            "52w_high":       _get("fiftyTwoWeekHigh"),
            "52w_low":        _get("fiftyTwoWeekLow"),
            "shares_out":     _get("sharesOutstanding"),
            "float_shares":   _get("floatShares"),
            "sector":         info.get("sector"),
            "industry":       info.get("industry"),
        }
