"""
XAGUSD Silver Analyser — FastAPI backend
- Fetches 4H OHLCV from yfinance, calculates pivots + indicators
- Calls Claude Code CLI for AI analysis (no separate API key needed)
- Schedules analysis every 4H automatically
- Serves the PWA from /pwa directory
"""
import json
import math
import os
import re
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import numpy as np
import pandas as pd
import uvicorn
from dotenv import load_dotenv
load_dotenv()
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── JSON sanitization ─────────────────────────────────────────────────────────

def sanitize(obj):
    """Recursively replace NaN/Inf floats with None so JSON serialization never crashes."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


# ── Database ───────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "analysis.db"


def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS analyses "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, data TEXT)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS subscriptions "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT UNIQUE, data TEXT)"
        )


def db_save(result: dict):
    clean = sanitize(result)
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO analyses (ts, symbol, data) VALUES (?,?,?)",
            (clean["timestamp"], clean["symbol"], json.dumps(clean)),
        )


def db_latest() -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT data FROM analyses ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return json.loads(row[0]) if row else None


def db_history(limit: int = 10) -> list:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT data FROM analyses ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def db_save_sub(sub: dict):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR REPLACE INTO subscriptions (endpoint, data) VALUES (?,?)",
            (sub["endpoint"], json.dumps(sub)),
        )


def db_subs() -> list:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT data FROM subscriptions").fetchall()
    return [json.loads(r[0]) for r in rows]


# ── Data fetching ──────────────────────────────────────────────────────────────


def _polygon_key() -> str:
    key = os.getenv("POLYGON_API_KEY", "")
    if not key:
        raise ValueError("POLYGON_API_KEY not set — add it to your .env file")
    return key


def _polygon_aggs(timespan: str, multiplier: int, days_back: int) -> pd.DataFrame:
    """Fetch XAGUSD OHLCV from Polygon for a given timespan/multiplier."""
    end   = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url   = f"https://api.polygon.io/v2/aggs/ticker/C:XAGUSD/range/{multiplier}/{timespan}/{start}/{end}"
    params = {"sort": "asc", "limit": 5000, "apiKey": _polygon_key()}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results)
    df["Date"] = pd.to_datetime(df["t"], unit="ms").dt.tz_localize(None)
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    return df[["Date", "Open", "High", "Low", "Close", "Volume"]].set_index("Date").sort_index()


def fetch_4h():
    """Fetch XAGUSD 4H bars from Polygon — falls back to 1H resampled if needed."""
    # Try native 4H first (90 days = ~270 bars for 24/5 forex)
    try:
        df = _polygon_aggs("hour", 4, 90)
        if len(df) >= 20:
            print(f"  Polygon 4H: {len(df)} bars")
            return df, "C:XAGUSD (Polygon 4H)"
        print(f"  Polygon 4H returned only {len(df)} bars — trying 1H resample")
    except Exception as e:
        print(f"  Polygon 4H failed: {e}")

    # Fallback: 1H data resampled to 4H
    try:
        df1 = _polygon_aggs("hour", 1, 90)
        if df1.empty:
            raise ValueError("No 1H data")
        df = df1.resample("4h").agg(
            Open=("Open", "first"), High=("High", "max"),
            Low=("Low", "min"),    Close=("Close", "last"),
            Volume=("Volume", "sum"),
        ).dropna()
        print(f"  Polygon 1H→4H resample: {len(df)} bars")
        return df, "C:XAGUSD (Polygon 1H→4H)"
    except Exception as e:
        print(f"  Polygon 1H fallback failed: {e}")

    return None, None


# ── Technical calculations ─────────────────────────────────────────────────────


def calc_pivots(df) -> dict:
    prev = df.iloc[-2]
    H, L, C = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    P = (H + L + C) / 3
    return {
        "P":  round(P, 3),
        "R1": round(2 * P - L, 3),
        "R2": round(P + (H - L), 3),
        "R3": round(H + 2 * (P - L), 3),
        "S1": round(2 * P - H, 3),
        "S2": round(P - (H - L), 3),
        "S3": round(L - 2 * (H - P), 3),
    }


def _rsi(series, n=14) -> float:
    d = series.diff()
    g = d.clip(lower=0).ewm(com=n - 1, min_periods=n).mean()
    l = (-d.clip(upper=0)).ewm(com=n - 1, min_periods=n).mean()
    rsi = 100 - 100 / (1 + g / l.replace(0, np.nan))
    return round(float(rsi.iloc[-1]), 2)


def calc_indicators(df) -> dict:
    c = df["Close"]
    lo14 = df["Low"].rolling(14).min()
    hi14 = df["High"].rolling(14).max()
    sk = 100 * (c - lo14) / (hi14 - lo14)
    ema12 = c.ewm(span=12).mean()
    ema26 = c.ewm(span=26).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9).mean()

    price = round(float(c.iloc[-1]), 3)
    prev  = round(float(c.iloc[-2]), 3)
    day7  = round(float(c.iloc[-7]), 3) if len(c) > 7 else prev

    return {
        "price":      price,
        "change_4h":  round((price - prev) / prev * 100, 2),
        "change_24h": round((price - day7) / day7 * 100, 2),
        "rsi":        _rsi(c),
        "stoch_k":    round(float(sk.iloc[-1]), 2),
        "stoch_d":    round(float(sk.rolling(3).mean().iloc[-1]), 2),
        "macd":       round(float(macd.iloc[-1]), 4),
        "macd_signal":round(float(sig.iloc[-1]), 4),
        "ma20":       round(float(c.rolling(20).mean().iloc[-1]), 3),
        "ma50":       round(float(c.rolling(50).mean().iloc[-1]), 3),
    }


# ── Claude Code CLI analysis ───────────────────────────────────────────────────


def claude_analyse(ind: dict, pivots: dict) -> dict:
    """Call `claude -p <prompt>` and parse JSON response."""
    prompt = (
        "You are a professional forex/commodities analyst. "
        "Analyse XAGUSD (Silver Spot) 4H chart data and return ONLY a JSON object — "
        "no markdown, no extra text, just the raw JSON.\n\n"
        f"Price: ${ind['price']}  4H: {ind['change_4h']}%  24H: {ind['change_24h']}%\n"
        f"Pivots — R3:{pivots['R3']} R2:{pivots['R2']} R1:{pivots['R1']} "
        f"P:{pivots['P']} S1:{pivots['S1']} S2:{pivots['S2']} S3:{pivots['S3']}\n"
        f"RSI:{ind['rsi']}  Stoch-K:{ind['stoch_k']}  Stoch-D:{ind['stoch_d']}  "
        f"MACD:{ind['macd']}  Signal:{ind['macd_signal']}  "
        f"MA20:{ind['ma20']}  MA50:{ind['ma50']}\n\n"
        "Return this exact JSON schema:\n"
        '{"bias":"BULLISH|BEARISH|NEUTRAL",'
        '"bias_strength":"STRONG|MODERATE|WEAK",'
        '"summary":"2 concise sentences",'
        '"key_level_label":"e.g. R2 resistance",'
        '"key_level_price":number,'
        '"target_bull":number,'
        '"target_bear":number,'
        '"stop_loss":number,'
        '"today_low":number,'
        '"today_high":number,'
        '"week_low":number,'
        '"week_high":number,'
        '"signals":["signal 1","signal 2","signal 3"],'
        '"risks":["risk 1","risk 2"]}'
    )
    try:
        res = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        raw = res.stdout.strip()
        # Strip markdown code fences if present
        if "```" in raw:
            raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
        # Extract the JSON object
        m = re.search(r"\{[\s\S]+\}", raw)
        if m:
            return json.loads(m.group())
        return {"error": "no_json", "raw": raw[:500], "bias": "NEUTRAL",
                "summary": "Could not parse AI response."}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "bias": "NEUTRAL", "summary": "Analysis timed out."}
    except Exception as e:
        return {"error": str(e), "bias": "NEUTRAL", "summary": "Analysis error."}


# ── Main analysis runner ───────────────────────────────────────────────────────


def run_analysis() -> Optional[dict]:
    df, source = fetch_4h()
    if df is None:
        return None
    pivots   = calc_pivots(df)
    ind      = calc_indicators(df)
    analysis = claude_analyse(ind, pivots)
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "symbol":    "XAGUSD",
        "source":    source,
        "indicators": ind,
        "pivots":    pivots,
        "analysis":  analysis,
    }


# ── Push notifications (optional — needs VAPID env vars) ──────────────────────

VAPID_PRIVATE = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL   = os.getenv("VAPID_EMAIL", "mailto:admin@xagusd.local")


def send_push(subs: list, result: dict):
    if not VAPID_PRIVATE or not subs:
        return
    try:
        from pywebpush import webpush, WebPushException  # type: ignore
        a = result.get("analysis", {})
        bias  = a.get("bias", "?")
        price = result["indicators"]["price"]
        payload = json.dumps({
            "title": f"XAGUSD 4H — {bias}",
            "body":  (f"${price}  "
                      f"Bull→{a.get('target_bull','?')}  "
                      f"Bear→{a.get('target_bear','?')}"),
            "icon":  "/icon-192.png",
            "badge": "/icon-192.png",
        })
        for sub in subs:
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE,
                    vapid_claims={"sub": VAPID_EMAIL},
                )
            except WebPushException:
                pass
    except ImportError:
        pass  # pywebpush not installed — push silently skipped


# ── Scheduler + FastAPI ────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()


async def _scheduled_run():
    result = run_analysis()
    if result:
        db_save(result)
        send_push(db_subs(), result)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db_init()
    print("Running initial analysis on startup…")
    result = run_analysis()
    if result:
        db_save(result)
        print(f"  → {result['symbol']} ${result['indicators']['price']} | "
              f"bias: {result['analysis'].get('bias','?')}")
    else:
        print("  → Could not fetch price data")
    # Fire at :05 past every 4H (00:05, 04:05, 08:05, 12:05, 16:05, 20:05 UTC)
    scheduler.add_job(_scheduled_run, CronTrigger(hour="0,4,8,12,16,20", minute=5))
    scheduler.start()
    print("Scheduler started — analysis runs every 4H automatically.")
    yield
    scheduler.shutdown()


app = FastAPI(title="XAGUSD Analyser API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class PushSub(BaseModel):
    endpoint: str
    keys: dict


@app.get("/api/analysis/latest")
def get_latest():
    d = db_latest()
    return sanitize(d) if d else JSONResponse({"error": "no analysis yet"}, status_code=404)


@app.get("/api/analysis/history")
def get_history(limit: int = 10):
    return db_history(limit)


@app.post("/api/analysis/run")
async def trigger_run(bg: BackgroundTasks):
    bg.add_task(_scheduled_run)
    return {"status": "started", "message": "Analysis running — check /api/analysis/latest in ~30s"}


@app.get("/api/vapid-public-key")
def vapid_key():
    return {"key": VAPID_PUBLIC}


@app.post("/api/subscribe")
def subscribe(sub: PushSub):
    db_save_sub(sub.dict())
    return {"status": "subscribed"}


# Serve PWA static files at root
_pwa = Path(__file__).parent / "pwa"
if _pwa.exists():
    app.mount("/", StaticFiles(directory=str(_pwa), html=True), name="pwa")

if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
