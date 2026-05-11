"""
Metal Spot Analyser - FastAPI backend
Switch metal by setting METAL=XAU (or XAG/XPT/XPD) in .env
"""
import json
import math
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import anthropic
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


# ── Metal configuration ────────────────────────────────────────────────────────
# Set METAL=XAU in .env to switch to gold; XPT=platinum; XPD=palladium

METALS = {
    "XAG": {
        "name":           "Silver",
        "symbol":         "XAGUSD",
        "badge":          "Ag",
        "polygon_ticker": "C:XAGUSD",
        "yahoo_ticker":   "SI=F",
        "goldapi_sym":    "XAG",
        "tv_symbol":      "OANDA:XAGUSD",
        "news_query":     "silver price XAG USD",
    },
    "XAU": {
        "name":           "Gold",
        "symbol":         "XAUUSD",
        "badge":          "Au",
        "polygon_ticker": "C:XAUUSD",
        "yahoo_ticker":   "GC=F",
        "goldapi_sym":    "XAU",
        "tv_symbol":      "OANDA:XAUUSD",
        "news_query":     "gold price XAU USD",
    },
    "XPT": {
        "name":           "Platinum",
        "symbol":         "XPTUSD",
        "badge":          "Pt",
        "polygon_ticker": "C:XPTUSD",
        "yahoo_ticker":   "PL=F",
        "goldapi_sym":    "XPT",
        "tv_symbol":      "OANDA:XPTUSD",
        "news_query":     "platinum price XPT USD",
    },
    "XPD": {
        "name":           "Palladium",
        "symbol":         "XPDUSD",
        "badge":          "Pd",
        "polygon_ticker": "C:XPDUSD",
        "yahoo_ticker":   "PA=F",
        "goldapi_sym":    "XPD",
        "tv_symbol":      "OANDA:XPDUSD",
        "news_query":     "palladium price XPD USD",
    },
}

METAL = os.getenv("METAL", "XAG").upper()
CFG   = METALS.get(METAL, METALS["XAG"])
print(f"[config] Metal = {METAL} ({CFG['name']}) | {CFG['symbol']}")


# ── JSON sanitization ──────────────────────────────────────────────────────────

def sanitize(obj):
    if isinstance(obj, dict):  return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [sanitize(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)): return None
    return obj


# ── Database ───────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "analysis.db"


def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("CREATE TABLE IF NOT EXISTS analyses "
                  "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, data TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS subscriptions "
                  "(id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT UNIQUE, data TEXT)")


def db_save(result: dict):
    clean = sanitize(result)
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT INTO analyses (ts, symbol, data) VALUES (?,?,?)",
                  (clean["timestamp"], clean["symbol"], json.dumps(clean)))


def db_latest() -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT data FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def db_history(limit: int = 10) -> list:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT data FROM analyses ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def db_save_sub(sub: dict):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR REPLACE INTO subscriptions (endpoint, data) VALUES (?,?)",
                  (sub["endpoint"], json.dumps(sub)))


def db_subs() -> list:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT data FROM subscriptions").fetchall()
    return [json.loads(r[0]) for r in rows]


# ── Polygon data fetching ──────────────────────────────────────────────────────

_ISTANBUL = "Europe/Istanbul"


def _polygon_key() -> str:
    key = os.getenv("POLYGON_API_KEY", "")
    if not key:
        raise ValueError("POLYGON_API_KEY not set in .env")
    return key


def _polygon_aggs(timespan: str, multiplier: int, days_back: int) -> pd.DataFrame:
    end   = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url   = (f"https://api.polygon.io/v2/aggs/ticker/{CFG['polygon_ticker']}"
             f"/range/{multiplier}/{timespan}/{start}/{end}")
    resp = requests.get(url, params={"sort": "asc", "limit": 5000,
                                     "apiKey": _polygon_key()}, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results)
    df["Date"] = (pd.to_datetime(df["t"], unit="ms", utc=True)
                  .dt.tz_convert(_ISTANBUL).dt.tz_localize(None))
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low",
                             "c": "Close", "v": "Volume"})
    return df[["Date", "Open", "High", "Low", "Close", "Volume"]].set_index("Date").sort_index()


def fetch_4h():
    """Fetch 4H OHLCV from Polygon aligned to Istanbul timezone."""
    try:
        df1 = _polygon_aggs("hour", 1, 90)
        if df1.empty:
            raise ValueError("No 1H data")
        df = df1.resample("4h").agg(
            Open=("Open", "first"), High=("High", "max"),
            Low=("Low", "min"),    Close=("Close", "last"),
            Volume=("Volume", "sum"),
        ).dropna()
        if len(df) >= 20:
            print(f"  Polygon 1H->4H ({CFG['symbol']}): {len(df)} bars")
            return df, f"{CFG['polygon_ticker']} (Polygon)"
        raise ValueError(f"Only {len(df)} bars after resample")
    except Exception as e:
        print(f"  Polygon 1H failed: {e}")

    try:
        df = _polygon_aggs("hour", 4, 90)
        if len(df) >= 10:
            print(f"  Polygon 4H ({CFG['symbol']}): {len(df)} bars")
            return df, f"{CFG['polygon_ticker']} (Polygon 4H)"
    except Exception as e:
        print(f"  Polygon 4H failed: {e}")

    return None, None


# ── Real-time price ────────────────────────────────────────────────────────────

_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_price_caches: dict = {}   # keyed by yahoo_ticker so each metal has its own TTL
_PRICE_TTL = 55            # seconds


def _yahoo_price(symbol: str) -> Optional[float]:
    url  = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    resp = requests.get(url, params={"interval": "1d", "range": "1d"},
                        headers=_YAHOO_HEADERS, timeout=8)
    resp.raise_for_status()
    meta = resp.json()["chart"]["result"][0]["meta"]
    p = meta.get("regularMarketPrice") or meta.get("previousClose")
    return round(float(p), 3) if p and float(p) > 1 else None


def get_realtime_price(cfg: dict = None) -> Optional[float]:
    """Live spot price, cached per-metal for 55s to avoid gateway timeouts."""
    cfg   = cfg or CFG
    cache = _price_caches.setdefault(cfg["yahoo_ticker"], {"price": None, "ts": 0.0})
    if cache["price"] and (time.time() - cache["ts"]) < _PRICE_TTL:
        return cache["price"]

    def _store(p: float) -> float:
        cache["price"] = p
        cache["ts"]    = time.time()
        return p

    # 1. Yahoo Finance (SI=F / GC=F / PL=F / PA=F)
    try:
        p = _yahoo_price(cfg["yahoo_ticker"])
        if p:
            print(f"  Live price: Yahoo {cfg['yahoo_ticker']} = ${p}")
            return _store(p)
    except Exception as e:
        print(f"  Yahoo {cfg['yahoo_ticker']} failed: {e}")

    # 2. gold-api.com fallback
    try:
        resp = requests.get(f"https://api.gold-api.com/price/{cfg['goldapi_sym']}", timeout=8)
        if resp.status_code == 200:
            p = resp.json().get("price")
            if p and float(p) > 1:
                print(f"  Live price: gold-api.com {cfg['goldapi_sym']} = ${round(float(p),3)}")
                return _store(round(float(p), 3))
    except Exception as e:
        print(f"  gold-api.com failed: {e}")

    # Return stale cache rather than nothing
    if cache["price"]:
        return cache["price"]

    print(f"  Live price unavailable for {cfg['symbol']} - using Polygon close")
    return None


# ── News fetching ──────────────────────────────────────────────────────────────

def fetch_news(cfg: dict = None, max_items: int = 6) -> list:
    """Recent news from Google News RSS - no API key required."""
    cfg  = cfg or CFG
    seen, items = set(), []
    for query in [cfg["news_query"], f"{cfg['name']} commodity market"]:
        if len(items) >= max_items:
            break
        try:
            url  = (f"https://news.google.com/rss/search"
                    f"?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en")
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item"):
                if len(items) >= max_items:
                    break
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link")  or "").strip()
                pub   = (item.findtext("pubDate") or "").strip()
                src   = (item.findtext("source") or "").strip()
                if not title or not link or title in seen:
                    continue
                seen.add(title)
                items.append({"title": title, "url": link,
                               "source": src, "published": pub})
        except Exception as e:
            print(f"  News fetch failed: {e}")
    print(f"  News: {len(items)} items for {cfg['name']}")
    return items


# ── Technical calculations ─────────────────────────────────────────────────────

def calc_pivots(df) -> dict:
    prev = df.iloc[-2]
    H, L, C = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    P = (H + L + C) / 3
    return {
        "P":  round(P, 3),
        "R1": round(2*P - L, 3),   "R2": round(P + (H-L), 3),
        "R3": round(H + 2*(P-L), 3),
        "S1": round(2*P - H, 3),   "S2": round(P - (H-L), 3),
        "S3": round(L - 2*(H-P), 3),
    }


def _rsi(series, n=14) -> float:
    d = series.diff()
    g = d.clip(lower=0).ewm(com=n-1, min_periods=n).mean()
    lo = (-d.clip(upper=0)).ewm(com=n-1, min_periods=n).mean()
    rsi = 100 - 100 / (1 + g / lo.replace(0, np.nan))
    return round(float(rsi.iloc[-1]), 2)


def calc_indicators(df) -> dict:
    c     = df["Close"]
    lo14  = df["Low"].rolling(14).min()
    hi14  = df["High"].rolling(14).max()
    sk    = 100 * (c - lo14) / (hi14 - lo14)
    ema12 = c.ewm(span=12).mean()
    ema26 = c.ewm(span=26).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9).mean()
    price = round(float(c.iloc[-1]), 3)
    prev  = round(float(c.iloc[-2]), 3)
    day7  = round(float(c.iloc[-7]), 3) if len(c) > 7 else prev
    return {
        "price":       price,
        "change_4h":   round((price - prev) / prev * 100, 2),
        "change_24h":  round((price - day7) / day7 * 100, 2),
        "rsi":         _rsi(c),
        "stoch_k":     round(float(sk.iloc[-1]), 2),
        "stoch_d":     round(float(sk.rolling(3).mean().iloc[-1]), 2),
        "macd":        round(float(macd.iloc[-1]), 4),
        "macd_signal": round(float(sig.iloc[-1]), 4),
        "ma20":        round(float(c.rolling(20).mean().iloc[-1]), 3),
        "ma50":        round(float(c.rolling(50).mean().iloc[-1]), 3),
    }


# ── Claude AI analysis ─────────────────────────────────────────────────────────

def claude_analyse(ind: dict, pivots: dict, news: list, cfg: dict = None) -> dict:
    cfg = cfg or CFG
    news_block = ""
    if news:
        headlines  = "\n".join(f"{i+1}. {n['title']}" for i, n in enumerate(news))
        news_block = f"\nRecent headlines (sentiment context):\n{headlines}\n"

    prompt = (
        f"You are a professional commodities analyst. "
        f"Analyse {cfg['symbol']} ({cfg['name']} Spot / USD) 4H chart data "
        f"and return ONLY a JSON object - no markdown, no extra text.\n\n"
        f"Price: ${ind['price']}  4H: {ind['change_4h']}%  24H: {ind['change_24h']}%\n"
        f"Pivots - R3:{pivots['R3']} R2:{pivots['R2']} R1:{pivots['R1']} "
        f"P:{pivots['P']} S1:{pivots['S1']} S2:{pivots['S2']} S3:{pivots['S3']}\n"
        f"RSI:{ind['rsi']}  Stoch-K:{ind['stoch_k']}  Stoch-D:{ind['stoch_d']}  "
        f"MACD:{ind['macd']}  Signal:{ind['macd_signal']}  "
        f"MA20:{ind['ma20']}  MA50:{ind['ma50']}\n"
        f"{news_block}\n"
        'Return exactly this JSON schema (numbers only, no units):\n'
        '{"bias":"BULLISH|BEARISH|NEUTRAL","bias_strength":"STRONG|MODERATE|WEAK",'
        '"summary":"2 concise sentences","key_level_label":"e.g. R2 resistance",'
        '"key_level_price":number,"target_bull":number,"target_bear":number,'
        '"stop_loss":number,"today_low":number,"today_high":number,'
        '"week_low":number,"week_high":number,'
        '"signals":["signal 1","signal 2","signal 3"],"risks":["risk 1","risk 2"],'
        '"news_sentiment":"BULLISH|BEARISH|NEUTRAL",'
        '"news_sentiment_reason":"one sentence explaining the news mood"}'
    )
    try:
        client = anthropic.Anthropic()
        msg    = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if "```" in raw:
            raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
        m = re.search(r"\{[\s\S]+\}", raw)
        if m:
            return json.loads(m.group())
        return {"error": "no_json", "bias": "NEUTRAL",
                "summary": "Could not parse AI response."}
    except Exception as e:
        return {"error": str(e), "bias": "NEUTRAL", "summary": "Analysis error."}


# ── Main analysis runner ───────────────────────────────────────────────────────

def run_analysis(cfg: dict = None) -> Optional[dict]:
    cfg = cfg or CFG
    df, source = fetch_4h()
    if df is None:
        return None

    pivots = calc_pivots(df)
    ind    = calc_indicators(df)

    # Overlay live price on top of stale Polygon historical close
    rt = get_realtime_price(cfg)
    if rt is not None:
        poly_ref = ind["price"]
        day7_ref = float(df["Close"].iloc[-7]) if len(df) > 7 else poly_ref
        ind["change_4h"]  = round((rt - poly_ref) / poly_ref * 100, 2)
        ind["change_24h"] = round((rt - day7_ref) / day7_ref * 100, 2)
        ind["price"]      = rt
        source            = source + " + live"

    news     = fetch_news(cfg)
    analysis = claude_analyse(ind, pivots, news, cfg)

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "symbol":    cfg["symbol"],
        "source":    source,
        "meta": {
            "name":      cfg["name"],
            "badge":     cfg["badge"],
            "tv_symbol": cfg["tv_symbol"],
        },
        "indicators": ind,
        "pivots":     pivots,
        "analysis":   analysis,
        "news":       news,
    }


# ── Push notifications ─────────────────────────────────────────────────────────

VAPID_PRIVATE = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL   = os.getenv("VAPID_EMAIL", "mailto:admin@local")


def send_push(subs: list, result: dict):
    if not VAPID_PRIVATE or not subs:
        return
    try:
        from pywebpush import webpush, WebPushException
        a     = result.get("analysis", {})
        price = result["indicators"]["price"]
        payload = json.dumps({
            "title": f"{result['symbol']} 4H - {a.get('bias', '?')}",
            "body":  (f"${price}  "
                      f"Bull: {a.get('target_bull','?')}  "
                      f"Bear: {a.get('target_bear','?')}"),
            "icon":  "/icon-192.svg",
        })
        for sub in subs:
            try:
                webpush(subscription_info=sub, data=payload,
                        vapid_private_key=VAPID_PRIVATE,
                        vapid_claims={"sub": VAPID_EMAIL})
            except WebPushException:
                pass
    except ImportError:
        pass


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
    print("Running initial analysis on startup...")
    result = run_analysis()
    if result:
        db_save(result)
        print(f"  >> {result['symbol']} ${result['indicators']['price']} "
              f"| bias: {result['analysis'].get('bias', '?')}")
    else:
        print("  >> Could not fetch price data")
    scheduler.add_job(_scheduled_run, CronTrigger(hour="0,4,8,12,16,20", minute=5))
    scheduler.start()
    print("Scheduler started - analysis every 4H.")
    yield
    scheduler.shutdown()


app = FastAPI(title=f"{CFG['name']} Spot Analyser", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


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
    return {"status": "started",
            "message": "Analysis running - check /api/analysis/latest in ~30s"}


@app.get("/api/price/live")
def live_price():
    p = get_realtime_price()
    if p is None:
        return JSONResponse({"error": "price unavailable"}, status_code=503)
    return {"price": p, "symbol": CFG["symbol"],
            "ts": datetime.utcnow().isoformat() + "Z"}


@app.get("/api/config")
def get_config():
    return {"metal": METAL, **CFG}


@app.get("/api/vapid-public-key")
def vapid_key():
    return {"key": VAPID_PUBLIC}


@app.post("/api/subscribe")
def subscribe(sub: PushSub):
    db_save_sub(sub.dict())
    return {"status": "subscribed"}


_pwa = Path(__file__).parent / "pwa"
if _pwa.exists():
    app.mount("/", StaticFiles(directory=str(_pwa), html=True), name="pwa")

if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
