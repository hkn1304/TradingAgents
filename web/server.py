"""
FastAPI application — all REST endpoints + WebSocket streaming.

Route map
─────────
GET  /api/config                      provider capabilities + available agents
GET  /api/markets/price               live price (5-10 s refresh)
GET  /api/markets/ohlcv               OHLCV bars
GET  /api/markets/indicators          technical indicators
GET  /api/markets/pivots              pivot levels
GET  /api/markets/news                news feed

GET  /api/kalman/{ticker}             dual Kalman signal (RW + CV)
GET  /api/kalman/{ticker}/history     signal history for comparison

POST /api/jobs                        submit full-pipeline job (Tab 3)
GET  /api/jobs/{session_id}           job status + queue position

POST /api/agents/run                  run specific agents immediately (Tab 2)
GET  /api/agents/run/{session_id}     agent-run status + reports

GET  /api/sessions                    session history
GET  /api/sessions/{session_id}       full session with reports + agent status
DELETE /api/sessions/{session_id}     delete session

GET  /api/sessions/{session_id}/chat    chat history
POST /api/sessions/{session_id}/chat   send message → get reply
GET  /api/sessions/{session_id}/summary compact JSON card (horizon=today|tomorrow|week|month)

GET  /api/templates                   list templates
POST /api/templates                   create template
PUT  /api/templates/{template_id}     update template
DELETE /api/templates/{template_id}   delete template

WS   /ws/{session_id}                 real-time stream of agent progress

GET  /api/portfolio/kalman            multi-ticker Kalman scan + allocation (Tab 4)

POST /api/mt5/connect                 connect to MT5 terminal (Tab 5)
DELETE /api/mt5/connect               disconnect from MT5
GET  /api/mt5/status                  connection status + account info + open positions
POST /api/mt5/config                  update execution config (risk %, thresholds, auto tickers)
POST /api/mt5/execute                 check concurrence + execute a single ticker signal
POST /api/mt5/close/{ticket}          close an open position by ticket number
GET  /api/mt5/log                     last 50 execution log entries

GET  /                                serves the 5-tab PWA (pwa/index.html)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from providers import (
    get_market_provider,
    ProviderFactory,
    Capability,
    OHLCVInterval,
)
from web.database import (
    init_db,
    session_create,
    session_get,
    session_list,
    session_delete,
    report_get_all,
    agent_status_get_all,
    chat_get_history,
    template_create,
    template_update,
    template_get,
    template_list,
    template_delete,
)
from web.stream import manager as ws_manager, set_loop
from web.job_runner import (
    start_worker,
    enqueue_full_pipeline,
    run_agents_immediate,
    cancel_session,
)
from web.chat import chat as chat_handler
from web.report_summarizer import get_or_build_summary, HORIZONS
from web.mt5_broker import MT5Broker
from web.execution_engine import ExecutionEngine

logger = logging.getLogger(__name__)

# ── MT5 singletons (created once, shared across requests) ────────────────────
_mt5_broker  = MT5Broker()
_exec_engine = ExecutionEngine(_mt5_broker)

# ── Lifespan ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    set_loop(asyncio.get_event_loop())
    start_worker()
    logger.info("TradingAgents web server ready")
    yield


# ── App ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="TradingAgents Web", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ───────────────────────────────────────────────────────

class JobSubmit(BaseModel):
    ticker:           str
    analysis_date:    str
    analysts:         list[str]          # e.g. ["market","fundamentals"]
    research_depth:   int   = 1          # 1 | 3 | 5
    forecast_horizon: str   = "1week"    # intraday|1day|1week|1month|longterm
    llm_provider:     str   = "anthropic"
    quick_llm:        Optional[str] = None
    deep_llm:         Optional[str] = None
    output_language:  str   = "English"
    analysis_horizon: str   = "week"     # today | tomorrow | week | month
    backend_url:      Optional[str] = None
    anthropic_effort: Optional[str] = None
    openai_reasoning_effort: Optional[str] = None
    google_thinking_level:   Optional[str] = None
    kalman_signal:    Optional[str] = None  # pre-formatted Kalman context injected into Trader prompt


class AgentRunSubmit(BaseModel):
    ticker:          str
    analysis_date:   str
    analysts:        list[str]           # Tier 1 only: market/social/news/fundamentals
    forecast_horizon: str  = "1week"    # intraday|1day|1week|1month|longterm
    llm_provider:    str   = "anthropic"
    quick_llm:       Optional[str] = None
    deep_llm:        Optional[str] = None
    output_language: str   = "English"
    analysis_horizon: str  = "week"      # today | tomorrow | week | month
    backend_url:     Optional[str] = None
    anthropic_effort: Optional[str] = None
    openai_reasoning_effort: Optional[str] = None
    google_thinking_level:   Optional[str] = None


class ChatMessage(BaseModel):
    message: str


class TemplateCreate(BaseModel):
    name:   str
    ticker: str
    config: dict = {}


class TemplateUpdate(BaseModel):
    name:   str
    ticker: str
    config: dict = {}


class MT5ConnectRequest(BaseModel):
    login:    int
    password: str
    server:   str


class MT5ConfigRequest(BaseModel):
    risk_pct:          Optional[float]     = None
    max_positions:     Optional[int]       = None
    min_kalman_score:  Optional[int]       = None
    agent_max_age_h:   Optional[float]     = None
    auto_tickers:      Optional[list[str]] = None
    enabled:           Optional[bool]      = None
    require_agent:     Optional[bool]      = None


class MT5ExecuteRequest(BaseModel):
    ticker:          str
    direction:       str            # 'bullish' | 'bearish'
    signal_strength: int
    models_agree:    bool
    atr:             Optional[float] = None


# ── /api/models ──────────────────────────────────────────────────────────────────

@app.get("/api/models")
def get_models():
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
    result = {}
    for provider, modes in MODEL_OPTIONS.items():
        result[provider] = {
            mode: [{"label": label, "value": value} for label, value in options]
            for mode, options in modes.items()
        }
    return result


# ── /api/config ───────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    provider_name = os.getenv("MARKET_DATA_PROVIDER", "yfinance")
    provider = get_market_provider()
    return {
        "market_provider": provider_name,
        "pipeline_provider": os.getenv("PIPELINE_DATA_PROVIDER", "yfinance"),
        "provider_capabilities": [c.value for c in provider.CAPABILITIES],
        "available_providers": ProviderFactory.capability_matrix(),
        "tier1_analysts": ["market", "social", "news", "fundamentals"],
        "tier2_agents": [
            "bull_researcher", "bear_researcher", "research_manager",
            "trader",
            "aggressive_analyst", "neutral_analyst", "conservative_analyst",
            "portfolio_manager",
        ],
        "mt5_defaults": {
            "login":    os.getenv("MT5_LOGIN", ""),
            "password": os.getenv("MT5_PASSWORD", ""),
            "server":   os.getenv("MT5_SERVER", ""),
        },
    }


# ── /api/markets/* ───────────────────────────────────────────────────────────────────

import time as _time

_market_cache: dict = {}  # key → (value, expires_at)

def _mc_get(key: str):
    entry = _market_cache.get(key)
    if entry and _time.monotonic() < entry[1]:
        return entry[0]
    return None

def _mc_set(key: str, value, ttl: float):
    _market_cache[key] = (value, _time.monotonic() + ttl)


@app.get("/api/markets/price")
def markets_price(ticker: str):
    key = f"price:{ticker}"
    cached = _mc_get(key)
    if cached is not None:
        return cached
    provider = get_market_provider()
    price = provider.get_price_live(ticker)
    if price is None:
        raise HTTPException(503, detail="Price unavailable")
    result = {"ticker": ticker, "price": price}
    _mc_set(key, result, 10)  # 10 s — matches UI refresh rate
    return result


@app.get("/api/markets/ohlcv")
def markets_ohlcv(
    ticker:   str,
    interval: str  = "1d",
    days:     int  = 365,
):
    from datetime import datetime, timedelta
    provider  = get_market_provider()
    end_date  = datetime.utcnow().strftime("%Y-%m-%d")
    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    interval_map = {
        "1m": OHLCVInterval.MIN_1, "5m": OHLCVInterval.MIN_5,
        "15m": OHLCVInterval.MIN_15, "30m": OHLCVInterval.MIN_30,
        "1h": OHLCVInterval.HOUR_1, "4h": OHLCVInterval.HOUR_4,
        "1d": OHLCVInterval.DAY_1,  "1wk": OHLCVInterval.WEEK_1,
    }
    iv = interval_map.get(interval, OHLCVInterval.DAY_1)

    df = provider.get_ohlcv(ticker, start_date, end_date, iv)
    if df.empty:
        raise HTTPException(404, detail=f"No OHLCV data for {ticker}")

    df["Date"] = df["Date"].astype(str)
    return {"ticker": ticker, "interval": interval, "bars": df.to_dict(orient="records")}


@app.get("/api/markets/indicators")
def markets_indicators(ticker: str, date: str | None = None):
    from datetime import datetime
    date = date or datetime.utcnow().strftime("%Y-%m-%d")
    key = f"indicators:{ticker}:{date}"
    cached = _mc_get(key)
    if cached is not None:
        return cached
    provider = get_market_provider()
    indicators = provider.get_indicators(ticker, date)
    result = {"ticker": ticker, "date": date, "indicators": indicators}
    _mc_set(key, result, 120)  # 2 min — intraday indicators don't change fast
    return result


@app.get("/api/markets/pivots")
def markets_pivots(ticker: str, interval: str = "1d", days: int = 90):
    from datetime import datetime, timedelta
    key = f"pivots:{ticker}:{interval}"
    cached = _mc_get(key)
    if cached is not None:
        return cached
    provider   = get_market_provider()
    end_date   = datetime.utcnow().strftime("%Y-%m-%d")
    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    interval_map = {
        "1h": OHLCVInterval.HOUR_1, "4h": OHLCVInterval.HOUR_4,
        "1d": OHLCVInterval.DAY_1,  "1wk": OHLCVInterval.WEEK_1,
    }
    iv = interval_map.get(interval, OHLCVInterval.DAY_1)
    df = provider.get_ohlcv(ticker, start_date, end_date, iv)
    if len(df) < 2:
        raise HTTPException(404, detail=f"Not enough OHLCV data for pivots on {ticker}")

    result = {"ticker": ticker, "interval": interval, "pivots": provider.calc_pivots(df)}
    _mc_set(key, result, 300)  # 5 min — daily pivots barely move intraday
    return result


@app.get("/api/markets/news")
def markets_news(ticker: str, limit: int = 6):
    key = f"news:{ticker}:{limit}"
    cached = _mc_get(key)
    if cached is not None:
        return cached
    provider = get_market_provider()
    if not provider.supports(Capability.NEWS):
        raise HTTPException(501, detail="Current provider does not support news")
    news = provider.get_news(ticker, max_items=limit)
    result = {"ticker": ticker, "news": news}
    _mc_set(key, result, 300)  # 5 min — news feed doesn't need instant refresh
    return result


# ── /api/kalman/{ticker} ─────────────────────────────────────────────────────────────

@app.get("/api/kalman/{ticker}")
def get_kalman(
    ticker: str,
    timeframe: str = "1d",
    days: int = 180,
):
    import re
    from datetime import datetime, timedelta
    from web.kalman import compute_both
    from web.database import kalman_signal_upsert, kalman_signal_history

    if not re.match(r'^[A-Z0-9.=\-]{1,20}$', ticker.upper()):
        raise HTTPException(400, detail="Invalid ticker")

    provider = get_market_provider()
    end_date   = datetime.utcnow().strftime("%Y-%m-%d")
    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    interval_map = {
        "1h": OHLCVInterval.HOUR_1,
        "4h": OHLCVInterval.HOUR_4,
        "1d": OHLCVInterval.DAY_1,
    }
    iv = interval_map.get(timeframe, OHLCVInterval.DAY_1)

    df = provider.get_ohlcv(ticker.upper(), start_date, end_date, iv)
    if df.empty or len(df) < 30:
        raise HTTPException(404, detail=f"Not enough data for {ticker}")

    result = compute_both(df, timeframe=timeframe)

    # Persist latest signal snapshot for both models
    ts_now = datetime.utcnow().isoformat()
    last_price = float(df['Close'].iloc[-1]) if 'Close' in df.columns else float(df['close'].iloc[-1])
    for model_name, sig in [('rw', result['rw']), ('cv', result['cv'])]:
        kalman_signal_upsert(
            ticker=ticker.upper(), timeframe=timeframe, model=model_name,
            ts=ts_now, price=last_price,
            filtered_price=sig['kalman_line'][-1],
            velocity=sig['velocity'][-1] if sig['velocity'] else None,
            bias=sig['bias'], signal=sig['signal'],
            vel_signal=sig.get('vel_signal'),
            z_score=sig['z_now'], gain=sig['gain_now'], regime=sig['regime']
        )

    return {
        "ticker":    ticker.upper(),
        "timeframe": timeframe,
        "n_bars":    result['n_bars'],
        "rw":        result['rw'],
        "cv":        result['cv'],
    }


@app.get("/api/kalman/{ticker}/history")
def get_kalman_history(ticker: str, timeframe: str = "1d", limit: int = 50):
    from web.database import kalman_signal_history
    return {
        "ticker":    ticker.upper(),
        "timeframe": timeframe,
        "history":   kalman_signal_history(ticker.upper(), timeframe, limit),
    }


# ── /api/portfolio/kalman ────────────────────────────────────────────────────

@app.get("/api/portfolio/kalman")
def portfolio_kalman(tickers: str, horizon: str = "1d"):
    """
    Multi-ticker Kalman scan for the Portfolio tab.
    tickers: comma-separated, up to 5, e.g. "AAPL,MSFT,GOOGL"
    horizon: 1h | 4h | 1d | 1w | 1m
    """
    import re
    import numpy as np
    from datetime import datetime, timedelta
    from web.kalman import compute_both

    ticker_list = [t.strip().upper() for t in tickers.split(',') if t.strip()][:5]
    if not ticker_list:
        raise HTTPException(400, detail="No tickers provided")
    for t in ticker_list:
        if not re.match(r'^[A-Z0-9.\-=]{1,20}$', t):
            raise HTTPException(400, detail=f"Invalid ticker: {t}")

    # horizon → (OHLCVInterval, lookback_days, kalman_timeframe_key)
    horizon_cfg = {
        "1h": (OHLCVInterval.MIN_15, 5,   "15m"),
        "4h": (OHLCVInterval.MIN_30, 14,  "30m"),
        "1d": (OHLCVInterval.HOUR_1, 30,  "1h"),
        "1w": (OHLCVInterval.HOUR_4, 60,  "4h"),
        "1m": (OHLCVInterval.DAY_1,  180, "1d"),
    }
    iv, lookback_days, tf_key = horizon_cfg.get(horizon, horizon_cfg["1d"])
    bar_label = tf_key

    provider   = get_market_provider()
    end_date   = datetime.utcnow().strftime("%Y-%m-%d")
    start_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    results = []
    for ticker in ticker_list:
        try:
            df = provider.get_ohlcv(ticker, start_date, end_date, iv)
            if df.empty or len(df) < 30:
                results.append(_port_error(ticker, "Insufficient data"))
                continue

            both = compute_both(df, timeframe=tf_key)
            rw   = both['rw']
            cv   = both['cv']
            n    = both['n_bars']
            window = min(30, n)

            # Normalise column names
            df2 = df.copy()
            if isinstance(df2.columns, __import__('pandas').MultiIndex):
                df2.columns = [c[0].lower() for c in df2.columns]
            else:
                df2.columns = [c.lower() if isinstance(c, str) else str(c).lower() for c in df2.columns]

            closes   = df2['close'].values.astype(float)[-window:]
            k_line   = np.array(rw['kalman_line'])[-window:]
            slope    = np.array(rw['slope'])[-window:]
            velocity = np.array(cv['velocity'])[-window:] if cv['velocity'] else np.zeros(window)

            buy_n = sell_n = neu_n = 0
            for i in range(window):
                is_buy  = closes[i] > k_line[i] and slope[i] > 0 and velocity[i] > 0
                is_sell = closes[i] < k_line[i] and slope[i] < 0 and velocity[i] < 0
                if is_buy:    buy_n  += 1
                elif is_sell: sell_n += 1
                else:         neu_n  += 1

            buy_r  = buy_n  / window
            sell_r = sell_n / window
            neu_r  = neu_n  / window

            direction_score = buy_r - sell_r
            if abs(direction_score) < 0.10:
                direction = "neutral"
            elif direction_score > 0:
                direction = "bullish"
            else:
                direction = "bearish"

            # Effective model signals (cross > bias; CV prefers vel if recent)
            rw_cross = rw.get('signal')
            cv_cross = cv.get('signal')
            cv_vel   = cv.get('vel_signal')
            cv_vel_age = cv.get('vel_signal_age_bars')

            def _eff(cross, bias):
                if cross: return cross
                return 'buy' if bias == 'bullish' else ('sell' if bias == 'bearish' else None)

            rw_eff = _eff(rw_cross, rw['bias'])
            cv_eff = (cv_vel if (cv_vel and cv_vel_age is not None and cv_vel_age < 15)
                      else _eff(cv_cross, cv['bias']))
            models_agree = bool(rw_eff and cv_eff and rw_eff == cv_eff)
            # What direction do the models agree on (if they do)?
            agreed_side = rw_eff if models_agree else None  # 'buy' | 'sell' | None

            # Composite score (0–100)
            score = max(buy_r, sell_r) * 50
            # Agreement bonus only when models align WITH the bar-state direction
            if models_agree and agreed_side == ('buy' if direction == 'bullish' else 'sell'):
                score += 15
            elif models_agree:
                # Models agree but against the historical trend — mild penalty
                score -= 5
            if rw['regime'] == 'trending': score += 10
            cross_age = rw.get('signal_age_bars')
            if cross_age is not None and cross_age < 5: score += 10
            z = rw['z_now']
            if abs(z) < 1.5:  score += 10
            if abs(z) > 2.5:  score -= 15
            score = int(max(0, min(100, round(score))))

            # Model signals take priority over bar-state direction when they agree
            if agreed_side and score >= 40:
                if agreed_side == 'buy':
                    sig_label = "strong_buy" if score >= 65 else "buy"
                else:
                    sig_label = "strong_sell" if score >= 65 else "sell"
            elif direction == "neutral" or score < 40:
                sig_label = "neutral"
            elif direction == "bullish":
                sig_label = "buy"
            else:
                sig_label = "sell"

            last_price = float(df2['close'].iloc[-1])

            results.append({
                "ticker":           ticker,
                "error":            None,
                "price":            round(last_price, 4),
                "signal":           sig_label,
                "signal_strength":  score,
                "direction":        direction,
                "buy_ratio":        round(buy_r,  3),
                "sell_ratio":       round(sell_r, 3),
                "neutral_ratio":    round(neu_r,  3),
                "rw_signal":        rw_eff,
                "cv_signal":        cv_eff,
                "models_agree":     models_agree,
                "regime":           rw['regime'],
                "z_now":            rw['z_now'],
                "sparkline_closes": closes.tolist(),
                "sparkline_kalman": k_line.tolist(),
            })

        except Exception as exc:
            logger.error(f"Portfolio Kalman error for {ticker}: {exc}")
            results.append(_port_error(ticker, str(exc)))

    # Score-weighted allocation among confirmed BUY tickers (score >= 45)
    buy_results = [r for r in results if not r.get('error')
                   and r['signal'] in ('buy', 'strong_buy') and r['signal_strength'] >= 45]
    total_score = sum(r['signal_strength'] for r in buy_results) or 1
    allocation  = {r['ticker']: (round(r['signal_strength'] / total_score, 4)
                                 if r in buy_results else 0.0)
                   for r in results}

    return {"horizon": horizon, "bar_size": bar_label, "results": results, "allocation": allocation}


def _port_error(ticker: str, msg: str) -> dict:
    return {
        "ticker": ticker, "error": msg, "price": None,
        "signal": "neutral", "signal_strength": 0, "direction": "neutral",
        "buy_ratio": 0.0, "sell_ratio": 0.0, "neutral_ratio": 1.0,
        "rw_signal": None, "cv_signal": None, "models_agree": False,
        "regime": "ranging", "z_now": 0.0,
        "sparkline_closes": [], "sparkline_kalman": [],
    }


# ── /api/mt5/* (Tab 5 — execution) ──────────────────────────────────────────

@app.post("/api/mt5/connect", status_code=200)
def mt5_connect(body: MT5ConnectRequest):
    ok, msg = _mt5_broker.connect(body.login, body.password, body.server)
    if not ok:
        raise HTTPException(400, detail=msg)
    acct = _mt5_broker.get_account_info()
    return {
        "connected": True,
        "message":   msg,
        "account":   _acct_dict(acct),
    }


@app.delete("/api/mt5/connect", status_code=200)
def mt5_disconnect():
    _mt5_broker.disconnect()
    return {"connected": False}


@app.get("/api/mt5/status")
def mt5_status():
    acct      = _mt5_broker.get_account_info()
    positions = _mt5_broker.get_positions()
    return {
        "connected": _mt5_broker.connected,
        "account":   _acct_dict(acct),
        "positions": [_pos_dict(p) for p in positions],
        "config":    _exec_engine.get_config(),
    }


@app.post("/api/mt5/config")
def mt5_config(body: MT5ConfigRequest):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    _exec_engine.update_config(**updates)
    return {"config": _exec_engine.get_config()}


@app.post("/api/mt5/execute")
def mt5_execute(body: MT5ExecuteRequest):
    """
    Check concurrence for a single ticker + Kalman result, then execute if confirmed.
    The Kalman result fields come directly from the Portfolio tab scan.
    """
    kalman_result = {
        "direction":       body.direction,
        "signal_strength": body.signal_strength,
        "models_agree":    body.models_agree,
    }
    signal, reason = _exec_engine.check_concurrence(body.ticker.upper(), kalman_result)
    if signal is None:
        return {"executed": False, "reason": reason}
    entry = _exec_engine.execute_signal(signal, atr=body.atr)
    return {"executed": entry["success"], "log": entry}


@app.post("/api/mt5/close/{ticket}")
def mt5_close(ticket: int):
    result = _mt5_broker.close_position(ticket)
    if not result.success:
        raise HTTPException(400, detail=result.comment)
    return {"closed": True, "ticket": ticket, "comment": result.comment}


@app.get("/api/mt5/log")
def mt5_log(limit: int = 50):
    return {"log": _exec_engine.get_log(limit)}


def _acct_dict(acct) -> Optional[dict]:
    if acct is None:
        return None
    return {
        "login":       acct.login,
        "server":      acct.server,
        "balance":     acct.balance,
        "equity":      acct.equity,
        "margin":      acct.margin,
        "margin_free": acct.margin_free,
        "leverage":    acct.leverage,
        "currency":    acct.currency,
        "profit":      acct.profit,
    }


def _pos_dict(p) -> dict:
    return {
        "ticket":        p.ticket,
        "symbol":        p.symbol,
        "direction":     p.direction,
        "volume":        p.volume,
        "entry_price":   p.entry_price,
        "current_price": p.current_price,
        "sl":            p.sl,
        "tp":            p.tp,
        "profit":        p.profit,
        "comment":       p.comment,
        "open_time":     p.open_time,
    }


# ── /api/jobs/* (Tab 3 — full pipeline) ─────────────────────────────────────────────

@app.post("/api/jobs", status_code=202)
def submit_job(body: JobSubmit):
    session_id = str(uuid.uuid4())
    config = body.model_dump(exclude={"ticker", "analysis_date", "analysts"})

    session_create(
        session_id    = session_id,
        ticker        = body.ticker.upper(),
        analysis_date = body.analysis_date,
        mode          = "full",
        config        = config,
    )

    position = enqueue_full_pipeline(
        session_id        = session_id,
        ticker            = body.ticker.upper(),
        analysis_date     = body.analysis_date,
        selected_analysts = body.analysts,
        config            = config,
    )
    return {
        "session_id":     session_id,
        "status":         "queued",
        "queue_position": position,
    }


@app.get("/api/jobs/{session_id}")
def get_job(session_id: str):
    session = session_get(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")
    return {
        "session":      session,
        "agent_status": agent_status_get_all(session_id),
        "reports":      {k: bool(v) for k, v in report_get_all(session_id).items()},
    }


@app.delete("/api/jobs/{session_id}", status_code=200)
def cancel_job(session_id: str):
    cancelled = cancel_session(session_id)
    if not cancelled:
        raise HTTPException(404, detail="Session not found or already finished")
    return {"session_id": session_id, "status": "cancelling"}


# ── /api/agents/run/* (Tab 2 — selective agents) ─────────────────────────────────

@app.post("/api/agents/run", status_code=202)
def submit_agent_run(body: AgentRunSubmit):
    # Only Tier 1 analysts allowed here
    _TIER1 = {"market", "social", "news", "fundamentals"}
    bad = [a for a in body.analysts if a.lower() not in _TIER1]
    if bad:
        raise HTTPException(
            400,
            detail=f"Only Tier 1 analysts can be run here: {bad}. "
                   "Submit Tier 2 via /api/jobs."
        )

    session_id = str(uuid.uuid4())
    config = body.model_dump(exclude={"ticker", "analysis_date", "analysts"})

    session_create(
        session_id    = session_id,
        ticker        = body.ticker.upper(),
        analysis_date = body.analysis_date,
        mode          = "agents",
        config        = config,
    )
    run_agents_immediate(
        session_id        = session_id,
        ticker            = body.ticker.upper(),
        analysis_date     = body.analysis_date,
        selected_analysts = [a.lower() for a in body.analysts],
        config            = config,
    )
    return {"session_id": session_id, "status": "running"}


@app.get("/api/agents/run/{session_id}")
def get_agent_run(session_id: str):
    session = session_get(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")
    reports = report_get_all(session_id)
    # Tab 2 only exposes analyst-tier report sections
    analyst_sections = {
        k: v for k, v in reports.items()
        if k in ("market_report", "sentiment_report",
                 "news_report", "fundamentals_report")
    }
    return {
        "session":      session,
        "agent_status": agent_status_get_all(session_id),
        "reports":      analyst_sections,
    }


@app.delete("/api/agents/run/{session_id}", status_code=200)
def cancel_agent_run(session_id: str):
    cancelled = cancel_session(session_id)
    if not cancelled:
        raise HTTPException(404, detail="Session not found or already finished")
    return {"session_id": session_id, "status": "cancelling"}



# ── /api/sessions/* (history) ──────────────────────────────────────────────────────────

@app.get("/api/sessions")
def list_sessions(limit: int = 50):
    return {"sessions": session_list(limit=limit)}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    session = session_get(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")
    return {
        "session":      session,
        "reports":      report_get_all(session_id),
        "agent_status": agent_status_get_all(session_id),
    }


@app.delete("/api/sessions/{session_id}", status_code=204)
def delete_session(session_id: str):
    if not session_get(session_id):
        raise HTTPException(404, detail="Session not found")
    session_delete(session_id)


# ── /api/sessions/{id}/chat ──────────────────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/chat")
def get_chat(session_id: str):
    if not session_get(session_id):
        raise HTTPException(404, detail="Session not found")
    return {"messages": chat_get_history(session_id)}


@app.post("/api/sessions/{session_id}/chat")
async def post_chat(session_id: str, body: ChatMessage):
    if not session_get(session_id):
        raise HTTPException(404, detail="Session not found")
    reply = await chat_handler(session_id, body.message)
    return {"reply": reply}


# ── /api/sessions/{id}/summary ──────────────────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/summary")
def get_summary(session_id: str, horizon: str = "week", force: bool = False):
    """Return a compact JSON card (~2 KB) summarising all reports for the session.
    horizon: today | tomorrow | week | month
    force: bypass cache and re-extract (used while pipeline is still running)
    """
    session = session_get(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")
    if horizon not in HORIZONS:
        raise HTTPException(400, detail=f"horizon must be one of: {list(HORIZONS)}")

    all_reports = report_get_all(session_id)
    investment_state = session.get("investment_debate_state") or {}
    risk_state       = session.get("risk_debate_state") or {}

    reports_payload = {
        "ticker":            session.get("ticker", ""),
        "date":              session.get("analysis_date", ""),
        "market_report":     all_reports.get("market_report", ""),
        "news_report":       all_reports.get("news_report", ""),
        "sentiment_report":  all_reports.get("sentiment_report", ""),
        "fundamentals_report": all_reports.get("fundamentals_report", ""),
        "investment_debate": investment_state.get("history", "") if isinstance(investment_state, dict) else "",
        "risk_debate":       risk_state.get("history", "") if isinstance(risk_state, dict) else "",
        "trader_plan":       all_reports.get("trader_investment_plan", ""),
        "final_decision":    all_reports.get("final_trade_decision", ""),
    }

    summary = get_or_build_summary(session_id, reports_payload, horizon=horizon, force=force)
    return {"session_id": session_id, "horizon": horizon, "summary": summary}


# ── /api/templates/* ───────────────────────────────────────────────────────────────────

@app.get("/api/templates")
def get_templates():
    return {"templates": template_list()}


@app.get("/api/templates/{template_id}")
def get_template(template_id: str):
    t = template_get(template_id)
    if not t:
        raise HTTPException(404, detail="Template not found")
    return {"template": t}


@app.post("/api/templates", status_code=201)
def create_template(body: TemplateCreate):
    tid = str(uuid.uuid4())
    template_create(tid, body.name, body.ticker.upper(), body.config)
    return {"template_id": tid}


@app.put("/api/templates/{template_id}")
def update_template(template_id: str, body: TemplateUpdate):
    if not template_get(template_id):
        raise HTTPException(404, detail="Template not found")
    template_update(template_id, body.name, body.ticker.upper(), body.config)
    return {"status": "updated"}


@app.delete("/api/templates/{template_id}", status_code=204)
def delete_template_route(template_id: str):
    if not template_get(template_id):
        raise HTTPException(404, detail="Template not found")
    template_delete(template_id)


# ── WebSocket /ws/{session_id} ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    await ws_manager.connect(session_id, ws)
    # Send current state immediately so a reconnecting browser catches up
    session = session_get(session_id)
    if session:
        all_reports = report_get_all(session_id)
        # Tab 2 (agents mode) sends full text; Tab 3 sends boolean presence flags
        if session.get("mode") == "agents":
            reports_payload = {
                k: v for k, v in all_reports.items()
                if k in ("market_report", "sentiment_report",
                         "news_report", "fundamentals_report")
            }
        else:
            reports_payload = {k: bool(v) for k, v in all_reports.items()}
        await ws.send_json({
            "type":         "snapshot",
            "session":      session,
            "agent_status": agent_status_get_all(session_id),
            "reports":      reports_payload,
        })
    try:
        while True:
            # Keep connection alive; browser sends pings every 30 s
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(session_id, ws)


# ── Static PWA ───────────────────────────────────────────────────────────────────────

_PWA_DIR = Path(__file__).parent.parent / "pwa"
if _PWA_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_PWA_DIR), html=True), name="pwa")
