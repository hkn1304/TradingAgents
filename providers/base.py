"""Abstract base class and shared types for all market data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

import pandas as pd


class Capability(str, Enum):
    LIVE_PRICE       = "live_price"
    OHLCV_DAILY      = "ohlcv_daily"
    OHLCV_INTRADAY   = "ohlcv_intraday"   # sub-daily bars (1h, 4h, etc.)
    FUNDAMENTALS     = "fundamentals"
    NEWS             = "news"
    STOCKS           = "stocks"
    CRYPTO           = "crypto"
    FOREX            = "forex"
    METALS           = "metals"


class AssetClass(str, Enum):
    STOCK   = "stock"
    CRYPTO  = "crypto"
    FOREX   = "forex"
    METAL   = "metal"
    ETF     = "etf"
    UNKNOWN = "unknown"


class OHLCVInterval(str, Enum):
    MIN_1  = "1m"
    MIN_5  = "5m"
    MIN_15 = "15m"
    MIN_30 = "30m"
    HOUR_1 = "1h"
    HOUR_4 = "4h"
    DAY_1  = "1d"
    WEEK_1 = "1wk"


class ProviderError(Exception):
    pass


class DataProvider(ABC):
    """
    Abstract base for all market data providers.

    Canonical ticker format (what the UI and pipeline always use):
      Stocks/ETFs : AAPL, SPY, MSFT
      Metals      : XAUUSD, XAGUSD, XPTUSD, XPDUSD
      Crypto      : BTCUSD, ETHUSD, BNBUSD
      Forex       : EURUSD, GBPUSD, USDJPY

    Each provider's normalize_ticker() translates from canonical → its own format.
    """

    # Declare supported capabilities as a class-level set
    CAPABILITIES: set[Capability] = set()

    # ── Capability checks ─────────────────────────────────────────────────────

    def supports(self, capability: Capability) -> bool:
        return capability in self.CAPABILITIES

    def require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise ProviderError(
                f"{self.__class__.__name__} does not support {capability.value}"
            )

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def normalize_ticker(self, ticker: str) -> str:
        """Translate canonical ticker to this provider's internal format."""
        ...

    @abstractmethod
    def get_price_live(self, ticker: str) -> Optional[float]:
        """Return the latest available price. Returns None if unavailable."""
        ...

    @abstractmethod
    def get_ohlcv(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: OHLCVInterval = OHLCVInterval.DAY_1,
    ) -> pd.DataFrame:
        """
        Return OHLCV bars as a DataFrame.

        Columns: Date (datetime), Open, High, Low, Close, Volume (all float).
        Rows are sorted ascending by Date, timezone-naive.
        """
        ...

    @abstractmethod
    def get_indicators(self, ticker: str, date: str) -> dict:
        """
        Return current values of standard technical indicators as of date.

        Expected keys (None where not computable):
          rsi, macd, macds, macdh,
          close_50_sma, close_200_sma, close_10_ema,
          boll, boll_ub, boll_lb, atr,
          stoch_k, stoch_d
        """
        ...

    @abstractmethod
    def get_news(self, ticker: str, max_items: int = 6) -> list[dict]:
        """
        Return recent news items.

        Each item: {title, url, source, published}
        """
        ...

    @abstractmethod
    def get_fundamentals(self, ticker: str, date: str) -> dict:
        """
        Return fundamental snapshot as of date.

        Keys (subset depending on asset class):
          pe_ratio, forward_pe, market_cap, revenue, profit_margin,
          debt_to_equity, roe, eps, dividend_yield, beta,
          52w_high, 52w_low
        """
        ...

    # ── Shared utilities (provider-agnostic) ─────────────────────────────────

    @staticmethod
    def calc_pivots(df: pd.DataFrame) -> dict:
        """
        Classic floor pivot levels calculated from the previous completed bar.
        Works on any OHLCV DataFrame regardless of provider.
        """
        if df is None or len(df) < 2:
            return {}
        prev = df.iloc[-2]
        H = float(prev["High"])
        L = float(prev["Low"])
        C = float(prev["Close"])
        P = (H + L + C) / 3
        return {
            "P":  round(P, 4),
            "R1": round(2 * P - L, 4),
            "R2": round(P + (H - L), 4),
            "R3": round(H + 2 * (P - L), 4),
            "S1": round(2 * P - H, 4),
            "S2": round(P - (H - L), 4),
            "S3": round(L - 2 * (H - P), 4),
        }

    @staticmethod
    def detect_asset_class(ticker: str) -> AssetClass:
        """Best-effort asset class detection from a canonical ticker string."""
        t = ticker.upper().strip()

        _METALS = {"XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"}
        if t in _METALS or t.endswith("=F"):
            return AssetClass.METAL

        # Known crypto base currencies — checked before the generic forex heuristic
        # so that BTCUSD / ETHUSD are not misclassified as forex pairs.
        _CRYPTO_BASES = {
            "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE",
            "DOT", "AVAX", "MATIC", "LINK", "LTC", "BCH", "UNI",
            "ATOM", "FIL", "TRX", "ETC", "APT", "ARB", "OP",
        }
        _FIAT_TERMS = {"USD", "USDT", "USDC", "EUR", "GBP"}
        base = t[:-3] if t.endswith("USD") else t.split("-")[0]
        if base in _CRYPTO_BASES:
            return AssetClass.CRYPTO
        if "-USD" in t or "-USDT" in t:
            return AssetClass.CRYPTO
        if any(t.endswith(s) for s in ("USDT", "USDC")) and len(t) > 6:
            return AssetClass.CRYPTO

        # Forex: exactly 6 alpha characters (EURUSD, GBPJPY, etc.)
        if len(t) == 6 and t.isalpha():
            return AssetClass.FOREX

        # ETF heuristics
        _ETF_HINTS = {"SPY", "QQQ", "IWM", "GLD", "SLV", "USO", "XLE", "XLF"}
        if t in _ETF_HINTS or t.endswith("ETF"):
            return AssetClass.ETF

        return AssetClass.STOCK
