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

POST /api/jobs                        submit full-pipeline job (Tab 3)
GET  /api/jobs/{session_id}           job status + queue position

POST /api/agents/run                  run specific agents immediately (Tab 2)
GET  /api/agents/run/{session_id}     agent-run status + reports

GET  /api/sessions                    session history
GET  /api/sessions/{session_id}       full session with reports + agent status
DELETE /api/sessions/{session_id}     delete session

GET  /api/sessions/{session_id}/chat  chat history
POST /api/sessions/{session_id}/chat  send message → get reply

GET  /api/templates                   list templates
POST /api/templates                   create template
PUT  /api/templates/{template_id}     update template
DELETE /api/templates/{template_id}   delete template

WS   /ws/{session_id}                 real-time stream of agent progress

GET  /                                serves the 3-tab PWA (pwa/index.html)
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
)
from web.chat import chat as chat_handler

logger = logging.getLogger(__name__)

# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    set_loop(asyncio.get_event_loop())
    start_worker()
    logger.info("TradingAgents web server ready")
    yield


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="TradingAgents Web", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ──────────────────────────────────────────────────

class JobSubmit(BaseModel):
    ticker:           str
    analysis_date:    str
    analysts:         list[str]          # e.g. ["market","fundamentals"]
    research_depth:   int   = 1          # 1 | 3 | 5
    llm_provider:     str   = "anthropic"
    quick_llm:        Optional[str] = None
    deep_llm:         Optional[str] = None
    output_language:  str   = "English"
    backend_url:      Optional[str] = None
    anthropic_effort: Optional[str] = None
    openai_reasoning_effort: Optional[str] = None
    google_thinking_level:   Optional[str] = None


class AgentRunSubmit(BaseModel):
    ticker:          str
    analysis_date:   str
    analysts:        list[str]           # Tier 1 only: market/social/news/fundamentals
    llm_provider:    str   = "anthropic"
    quick_llm:       Optional[str] = None
    deep_llm:        Optional[str] = None
    output_language: str   = "English"
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


# ── /api/config ────────────────────────────────────────────────────────────────

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
    }


# ── /api/markets/* ─────────────────────────────────────────────────────────────

@app.get("/api/markets/price")
def markets_price(ticker: str):
    provider = get_market_provider()
    price = provider.get_price_live(ticker)
    if price is None:
        raise HTTPException(503, detail="Price unavailable")
    return {"ticker": ticker, "price": price}


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
    provider = get_market_provider()
    date = date or datetime.utcnow().strftime("%Y-%m-%d")
    indicators = provider.get_indicators(ticker, date)
    return {"ticker": ticker, "date": date, "indicators": indicators}


@app.get("/api/markets/pivots")
def markets_pivots(ticker: str, interval: str = "1d", days: int = 90):
    from datetime import datetime, timedelta
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

    return {"ticker": ticker, "interval": interval, "pivots": provider.calc_pivots(df)}


@app.get("/api/markets/news")
def markets_news(ticker: str, limit: int = 6):
    provider = get_market_provider()
    if not provider.supports(Capability.NEWS):
        raise HTTPException(501, detail="Current provider does not support news")
    news = provider.get_news(ticker, max_items=limit)
    return {"ticker": ticker, "news": news}


# ── /api/jobs/* (Tab 3 — full pipeline) ───────────────────────────────────────

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


# ── /api/agents/run/* (Tab 2 — selective agents) ───────────────────────────────

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


# ── /api/sessions/* (history) ──────────────────────────────────────────────────

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


# ── /api/sessions/{id}/chat ────────────────────────────────────────────────────

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


# ── /api/templates/* ───────────────────────────────────────────────────────────

@app.get("/api/templates")
def get_templates():
    return {"templates": template_list()}


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


# ── WebSocket /ws/{session_id} ─────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    await ws_manager.connect(session_id, ws)
    # Send current state immediately so a reconnecting browser catches up
    session = session_get(session_id)
    if session:
        await ws.send_json({
            "type":         "snapshot",
            "session":      session,
            "agent_status": agent_status_get_all(session_id),
            "reports":      {k: bool(v) for k, v in report_get_all(session_id).items()},
        })
    try:
        while True:
            # Keep connection alive; browser sends pings every 30 s
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(session_id, ws)


# ── Static PWA ─────────────────────────────────────────────────────────────────

_PWA_DIR = Path(__file__).parent.parent / "pwa"
if _PWA_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_PWA_DIR), html=True), name="pwa")
