"""Polygon.io data provider for TradingAgents."""

import os
import time
import requests
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
from typing import Annotated

from stockstats import wrap

from .stockstats_utils import _clean_dataframe
from .config import get_config
from .utils import safe_ticker_component

POLYGON_BASE = "https://api.polygon.io"

_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": (
        "50 SMA: A medium-term trend indicator. "
        "Usage: Identify trend direction and serve as dynamic support/resistance. "
        "Tips: It lags price; combine with faster indicators for timely signals."
    ),
    "close_200_sma": (
        "200 SMA: A long-term trend benchmark. "
        "Usage: Confirm overall market trend and identify golden/death cross setups. "
        "Tips: It reacts slowly; best for strategic trend confirmation."
    ),
    "close_10_ema": (
        "10 EMA: A responsive short-term average. "
        "Usage: Capture quick shifts in momentum and potential entry points. "
        "Tips: Prone to noise in choppy markets."
    ),
    "macd": (
        "MACD: Computes momentum via differences of EMAs. "
        "Usage: Look for crossovers and divergence as signals of trend changes."
    ),
    "macds": (
        "MACD Signal: An EMA smoothing of the MACD line. "
        "Usage: Use crossovers with the MACD line to trigger trades."
    ),
    "macdh": (
        "MACD Histogram: Shows the gap between the MACD line and its signal. "
        "Usage: Visualize momentum strength and spot divergence early."
    ),
    "rsi": (
        "RSI: Measures momentum to flag overbought/oversold conditions. "
        "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals."
    ),
    "boll": (
        "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
        "Usage: Acts as a dynamic benchmark for price movement."
    ),
    "boll_ub": (
        "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
        "Usage: Signals potential overbought conditions and breakout zones."
    ),
    "boll_lb": (
        "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
        "Usage: Indicates potential oversold conditions."
    ),
    "atr": (
        "ATR: Averages true range to measure volatility. "
        "Usage: Set stop-loss levels and adjust position sizes based on volatility."
    ),
    "vwma": (
        "VWMA: A moving average weighted by volume. "
        "Usage: Confirm trends by integrating price action with volume data."
    ),
    "mfi": (
        "MFI: The Money Flow Index uses both price and volume to measure buying/selling pressure. "
        "Usage: Identify overbought (>80) or oversold (<20) conditions."
    ),
}


def _get_api_key() -> str:
    key = os.getenv("POLYGON_API_KEY", "")
    if not key:
        raise ValueError("POLYGON_API_KEY not set in environment")
    return key


def _get(path: str, params: dict = None) -> dict:
    """GET from Polygon REST API with retry on 429."""
    params = params or {}
    params["apiKey"] = _get_api_key()
    for attempt in range(4):
        resp = requests.get(f"{POLYGON_BASE}{path}", params=params, timeout=30)
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)
            print(f"[polygon] rate limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Polygon rate limit exceeded after retries")


_FOREX_PAIRS = {
    # Silver, Gold, other common commodity/forex codes without exchange prefix
    "XAGUSD", "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "NZDUSD", "USDCAD", "USDCNH",
}


def _polygon_ticker(symbol: str) -> str:
    """Return the Polygon ticker string, adding C: prefix for known forex pairs."""
    s = symbol.upper()
    if s in _FOREX_PAIRS or (len(s) == 6 and s.isalpha()):
        return f"C:{s}"
    return s


def _fetch_aggs(symbol: str, from_date: str, to_date: str) -> pd.DataFrame:
    """Fetch daily OHLCV aggregates from Polygon (adjusted for equities, raw for forex)."""
    poly_ticker = _polygon_ticker(symbol)
    is_forex = poly_ticker.startswith("C:")
    path = f"/v2/aggs/ticker/{poly_ticker}/range/1/day/{from_date}/{to_date}"
    params = {"sort": "asc", "limit": 50000}
    if not is_forex:
        params["adjusted"] = "true"
    data = _get(path, params)
    results = data.get("results", [])
    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df["Date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None).dt.normalize()
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    return df[["Date", "Open", "High", "Low", "Close", "Volume"]].sort_values("Date").reset_index(drop=True)


def load_ohlcv_polygon(symbol: str, curr_date: str) -> pd.DataFrame:
    """Load 5y of daily OHLCV from Polygon with file cache, filtered to curr_date."""
    safe_symbol = safe_ticker_component(symbol)
    config = get_config()

    today = pd.Timestamp.today()
    start_str = (today - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    cache_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-Polygon-data-{start_str}-{end_str}.csv",
    )

    if os.path.exists(cache_file):
        data = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
    else:
        data = _fetch_aggs(symbol, start_str, end_str)
        if data.empty:
            raise ValueError(f"No Polygon OHLCV data for {symbol}")
        data.to_csv(cache_file, index=False, encoding="utf-8")

    data = _clean_dataframe(data)
    return data[data["Date"] <= pd.to_datetime(curr_date)]


# ---------------------------------------------------------------------------
# Public API — signatures match the yfinance equivalents in interface.py
# ---------------------------------------------------------------------------

def get_stock_data(
    symbol: Annotated[str, "ticker symbol"],
    start_date: Annotated[str, "start date YYYY-MM-DD"],
    end_date: Annotated[str, "end date YYYY-MM-DD"],
) -> str:
    """Return CSV-formatted OHLCV data from Polygon.io (adjusted prices)."""
    df = _fetch_aggs(symbol, start_date, end_date)
    if df.empty:
        return f"No data found for symbol '{symbol}' between {start_date} and {end_date}"

    for col in ("Open", "High", "Low", "Close"):
        df[col] = df[col].round(2)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

    header = (
        f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
        f"# Total records: {len(df)}\n"
        f"# Data source: Polygon.io (adjusted)\n"
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )
    return header + df.to_csv(index=False)


def get_indicators(
    symbol: Annotated[str, "ticker symbol"],
    indicator: Annotated[str, "stockstats indicator name"],
    curr_date: Annotated[str, "current trading date YYYY-MM-DD"],
    look_back_days: Annotated[int, "number of days to look back"],
) -> str:
    """Return indicator values over a look-back window using Polygon OHLCV data."""
    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator '{indicator}' not supported. Choose from: {list(_INDICATOR_DESCRIPTIONS)}"
        )

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before_dt = curr_dt - relativedelta(days=look_back_days)

    data = load_ohlcv_polygon(symbol, curr_date)
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[indicator]  # trigger stockstats calculation

    date_map = {
        row["Date"]: (str(row[indicator]) if not pd.isna(row[indicator]) else "N/A")
        for _, row in df.iterrows()
    }

    ind_lines = ""
    current = curr_dt
    while current >= before_dt:
        ds = current.strftime("%Y-%m-%d")
        ind_lines += f"{ds}: {date_map.get(ds, 'N/A: Not a trading day')}\n"
        current -= relativedelta(days=1)

    return (
        f"## {indicator} values from {before_dt.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + ind_lines
        + f"\n\n{_INDICATOR_DESCRIPTIONS[indicator]}"
    )


def get_news(
    ticker: Annotated[str, "stock ticker symbol"],
    start_date: Annotated[str, "start date YYYY-MM-DD"],
    end_date: Annotated[str, "end date YYYY-MM-DD"],
) -> str:
    """Return recent news for a ticker from Polygon.io."""
    try:
        params = {
            "ticker": ticker.upper(),
            "published_utc.gte": start_date,
            "published_utc.lte": end_date + "T23:59:59Z",
            "order": "desc",
            "limit": 50,
            "sort": "published_utc",
        }
        data = _get("/v2/reference/news", params)
        articles = data.get("results", [])

        if not articles:
            return f"No news found for {ticker} between {start_date} and {end_date}"

        news_str = ""
        for a in articles:
            title = a.get("title", "No title")
            publisher = a.get("publisher", {}).get("name", "Unknown")
            description = a.get("description", "")
            url = a.get("article_url", "")
            news_str += f"### {title} (source: {publisher})\n"
            if description:
                news_str += f"{description}\n"
            if url:
                news_str += f"Link: {url}\n"
            news_str += "\n"

        return f"## {ticker} News, from {start_date} to {end_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching Polygon news for {ticker}: {e}"


def get_global_news(
    curr_date: Annotated[str, "current date YYYY-MM-DD"],
    look_back_days: Annotated[int, "days to look back"] = 7,
    limit: Annotated[int, "max articles"] = 10,
) -> str:
    """Return broad market news from Polygon.io (top tickers as proxy)."""
    try:
        start_dt = datetime.strptime(curr_date, "%Y-%m-%d") - relativedelta(days=look_back_days)
        start_date = start_dt.strftime("%Y-%m-%d")

        # Proxy for macro news: major index/ETF tickers
        macro_tickers = ["SPY", "QQQ", "DIA", "TLT", "GLD"]
        seen_titles: set = set()
        articles = []

        for proxy in macro_tickers:
            if len(articles) >= limit:
                break
            params = {
                "ticker": proxy,
                "published_utc.gte": start_date,
                "published_utc.lte": curr_date + "T23:59:59Z",
                "order": "desc",
                "limit": 10,
                "sort": "published_utc",
            }
            try:
                data = _get("/v2/reference/news", params)
                for a in data.get("results", []):
                    t = a.get("title", "")
                    if t and t not in seen_titles:
                        seen_titles.add(t)
                        articles.append(a)
            except Exception:
                continue

        if not articles:
            return f"No global news found for {curr_date}"

        news_str = ""
        for a in articles[:limit]:
            title = a.get("title", "No title")
            publisher = a.get("publisher", {}).get("name", "Unknown")
            description = a.get("description", "")
            url = a.get("article_url", "")
            news_str += f"### {title} (source: {publisher})\n"
            if description:
                news_str += f"{description}\n"
            if url:
                news_str += f"Link: {url}\n"
            news_str += "\n"

        return f"## Global Market News, from {start_date} to {curr_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching Polygon global news: {e}"
