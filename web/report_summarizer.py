"""
Convert full-length agent report text into a compact, structured JSON payload
suitable for phone-app transmission (~2 KB vs ~50 KB full text).

Uses Claude Haiku (cheap, fast) to extract the structured data.
Result is cached per session in memory.
"""
from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# ── JSON schema template sent to Claude ───────────────────────────────────────

# Valid horizon values shown in the UI
HORIZONS = {
    "today":    "Today (intraday/end-of-day)",
    "tomorrow": "Tomorrow (next session)",
    "week":     "This Week (5 trading days)",
    "month":    "This Month (3-4 weeks)",
}

_SCHEMA = {
    "meta": {
        "ticker": "string",
        "asset": "string — human-readable e.g. 'Silver spot price (XAG/USD)'",
        "date": "YYYY-MM-DD",
        "horizon": "today | tomorrow | week | month",
    },
    "signal": {
        "action": "BUY | SELL | HOLD | OVERWEIGHT | UNDERWEIGHT",
        "bias": "bullish | bearish | neutral | mixed",
        "confidence": "low | medium | medium-high | high",
        "horizon_note": "string — 1 sentence summarising conviction for the selected horizon",
    },
    "outlooks": {
        "today":    {"direction": "bullish | bearish | neutral", "range_low": "number", "range_high": "number", "summary": "1 concise sentence", "key_event": "string — single most important catalyst today"},
        "tomorrow": {"direction": "bullish | bearish | neutral", "range_low": "number", "range_high": "number", "summary": "1 concise sentence", "key_event": "string"},
        "week":     {"direction": "bullish | bearish | neutral", "range_low": "number", "range_high": "number", "summary": "1 concise sentence", "key_event": "string"},
        "month":    {"direction": "bullish | bearish | neutral", "range_low": "number", "range_high": "number", "summary": "1 concise sentence", "key_event": "string"},
    },
    "price_levels": {
        "current": "number",
        "entry_zone": ["number_low", "number_high"],
        "stop_loss": "number",
        "targets": [
            {"price": "number", "label": "short-term | medium-term | long-term", "timeframe": "string"}
        ],
    },
    "indicators": {
        "rsi": {"value": "number", "status": "oversold | neutral | near_overbought | overbought"},
        "macd": {"value": "number", "status": "bearish | neutral | bullish"},
        "trend": {"status": "downtrend | sideways | uptrend"},
        "volatility": {"status": "low | normal | high | extreme", "atr": "number or null"},
        "ma50_vs_price": "above | below",
        "ma200_vs_price": "above | below",
    },
    "analysts": {
        "market": {
            "signal": "BUY | SELL | HOLD | N/A",
            "bias": "bullish | bearish | neutral",
            "score": "1-10",
            "highlights": ["string — max 10 words each, 3 bullets"],
            "short_term": "string — 1 sentence",
            "long_term": "string — 1 sentence",
        },
        "news": {
            "signal": "BUY | SELL | HOLD | NEUTRAL | N/A",
            "bias": "bullish | bearish | neutral",
            "score": "1-10",
            "highlights": ["string — max 10 words each, 3 bullets"],
            "short_term": "string — 1 sentence",
            "long_term": "string — 1 sentence",
        },
        "social": {
            "signal": "BUY | SELL | HOLD | NEUTRAL | N/A",
            "bias": "bullish | bearish | neutral",
            "score": "1-10",
            "highlights": ["string — max 10 words each, 3 bullets"],
            "short_term": "string — 1 sentence",
            "long_term": "string — 1 sentence",
        },
        "fundamentals": {
            "signal": "BUY | SELL | HOLD | OVERWEIGHT | UNDERWEIGHT | N/A",
            "bias": "bullish | bearish | neutral",
            "score": "1-10",
            "highlights": ["string — max 10 words each, 3 bullets"],
            "short_term": "string — 1 sentence",
            "long_term": "string — 1 sentence",
        },
    },
    "debate": {
        "verdict": "BUY | SELL | HOLD | OVERWEIGHT | UNDERWEIGHT",
        "consensus": "string — 5-8 words",
        "bull_score": "1-10",
        "bear_score": "1-10",
        "key_points": ["string — 3 bullets, max 12 words each"],
        "stop_thesis": "string — what would invalidate the bull case",
    },
    "risk": {
        "score": "1-10 (10 = highest risk)",
        "level": "low | medium | medium-high | high",
        "key_risks": ["string — 3 bullets, max 8 words each"],
        "catalysts_to_watch": ["string — 3 items, max 6 words each"],
    },
    "scenarios": [
        {
            "label": "string — e.g. 'Bull Case' | 'Base Case' | 'Bear Case'",
            "trigger": "string — what event/condition triggers this scenario (max 12 words)",
            "price_target": "number — where price goes in this scenario",
            "direction": "up | down | sideways",
            "probability": "low | medium | high",
            "summary": "string — 1 concise sentence describing the scenario outcome",
        }
    ],
    "final": {
        "action": "BUY | SELL | HOLD",
        "entry": "number",
        "stop_loss": "number",
        "position_pct": "number — % of portfolio",
        "note": "string — 1 sentence execution note",
    },
}

_EXTRACTION_PROMPT = """You are a financial data extractor. Given agent analysis reports, extract a compact JSON summary.

IMPORTANT RULES:
- Output ONLY valid JSON — no markdown, no code fences, no prose
- All strings must be concise (highlights max 12 words, sentences max 20 words)
- Use null for any field that cannot be determined from the reports
- Numbers must be actual numbers (not strings)
- For the "outlooks" object, provide separate price-range and directional forecasts for each of the 4 horizons
  (today, tomorrow, week, month) — extrapolate from the reports even if not explicit
- The selected analysis horizon is: {horizon} — weight the primary signal toward this timeframe
- Preserve the exact JSON structure below

TARGET SCHEMA:
{schema}

REPORTS TO SUMMARISE:
{reports}

Output the JSON now:"""


# ── In-memory cache {session_id: summary_dict} ────────────────────────────────
_summary_cache: dict[str, dict] = {}


def _call_google_gemini(prompt: str) -> Optional[str]:
    """Call Google Gemini Flash as a fallback summarizer."""
    google_key = os.environ.get("GOOGLE_API_KEY")
    if not google_key:
        return None
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.messages import HumanMessage
        llm = ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key=google_key)
        response = llm.invoke([HumanMessage(content=prompt)])
        return response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        logger.error("Google Gemini fallback failed: %s", exc)
        return None


def _call_deepseek(prompt: str) -> Optional[str]:
    """Call DeepSeek V3 (cheapest, non-thinking) as a final fallback summarizer."""
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if not deepseek_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=deepseek_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content
    except Exception as exc:
        logger.error("DeepSeek summary fallback failed: %s", exc)
        return None


def build_summary(
    ticker: str,
    date: str,
    horizon: str = "week",
    market_report: str = "",
    news_report: str = "",
    sentiment_report: str = "",
    fundamentals_report: str = "",
    investment_debate: str = "",
    risk_debate: str = "",
    trader_plan: str = "",
    final_decision: str = "",
) -> dict:
    """Call Claude Haiku to extract structured summary from raw report texts."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    except Exception as exc:
        logger.error("Cannot initialise Anthropic client: %s", exc)
        return _fallback_summary(ticker, date, final_decision)

    reports_block = "\n\n".join(filter(None, [
        f"=== MARKET ANALYSIS ===\n{market_report}"        if market_report else "",
        f"=== NEWS ANALYSIS ===\n{news_report}"            if news_report else "",
        f"=== SOCIAL SENTIMENT ===\n{sentiment_report}"    if sentiment_report else "",
        f"=== FUNDAMENTALS ===\n{fundamentals_report}"     if fundamentals_report else "",
        f"=== BULL/BEAR DEBATE ===\n{investment_debate}"   if investment_debate else "",
        f"=== RISK & PORTFOLIO ===\n{risk_debate}"         if risk_debate else "",
        f"=== TRADER PLAN ===\n{trader_plan}"              if trader_plan else "",
        f"=== FINAL DECISION ===\n{final_decision}"        if final_decision else "",
    ]))

    prompt = _EXTRACTION_PROMPT.format(
        schema=json.dumps(_SCHEMA, indent=2),
        reports=reports_block[:12000],
        horizon=HORIZONS.get(horizon, horizon),
    )

    # Try Anthropic Haiku → Google Gemini → DeepSeek V3 (each cheaper/free-er)
    raw = None
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
    except Exception as exc:
        err_str = str(exc)
        if "credit balance" in err_str or "402" in err_str or "400" in err_str:
            logger.warning("Anthropic credits exhausted, trying Google Gemini for summary")
            raw = _call_google_gemini(prompt)
            if raw is None:
                logger.warning("Google Gemini unavailable, trying DeepSeek V3 for summary")
                raw = _call_deepseek(prompt)
        else:
            logger.error("Summary extraction failed: %s", exc)

    if raw is None:
        return _fallback_summary(ticker, date, final_decision)

    try:
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        result["_ok"] = True
        return result
    except json.JSONDecodeError as exc:
        logger.error("Summary JSON parse failed: %s", exc)
        return _fallback_summary(ticker, date, final_decision)


def _fallback_summary(ticker: str, date: str, final_decision: str) -> dict:
    """Minimal fallback when extraction fails."""
    action = "HOLD"
    if final_decision:
        upper = final_decision.upper()
        if "BUY" in upper:
            action = "BUY"
        elif "SELL" in upper:
            action = "SELL"
    return {
        "meta": {"ticker": ticker, "asset": ticker, "date": date},
        "signal": {"action": action, "bias": "neutral", "confidence": "low", "horizon": "unknown"},
        "price_levels": {"current": None, "entry_zone": [None, None], "stop_loss": None, "targets": []},
        "indicators": {
            "rsi": {"value": None, "status": "neutral"},
            "macd": {"value": None, "status": "neutral"},
            "trend": {"status": "sideways"},
            "volatility": {"status": "normal", "atr": None},
            "ma50_vs_price": None,
            "ma200_vs_price": None,
        },
        "analysts": {k: {"signal": "N/A", "bias": "neutral", "score": 5, "highlights": [], "short_term": "", "long_term": ""} for k in ("market", "news", "social", "fundamentals")},
        "debate": {"verdict": action, "consensus": "Insufficient data", "bull_score": 5, "bear_score": 5, "key_points": [], "stop_thesis": ""},
        "risk": {"score": 5, "level": "medium", "key_risks": [], "catalysts_to_watch": []},
        "final": {"action": action, "entry": None, "stop_loss": None, "position_pct": None, "note": ""},
    }


def get_or_build_summary(session_id: str, reports: dict, horizon: str = "week", force: bool = False) -> dict:
    """Return cached summary or build a new one. Cache key includes horizon.
    force=True skips cache read so a fresh extraction is done (used mid-pipeline).
    """
    cache_key = f"{session_id}:{horizon}"
    if not force and cache_key in _summary_cache:
        return _summary_cache[cache_key]
    summary = build_summary(horizon=horizon, **reports)
    # Only cache successful extractions — let failures retry on the next call
    if summary.get("_ok"):
        _summary_cache[cache_key] = summary
    return summary


def invalidate(session_id: str) -> None:
    _summary_cache.pop(session_id, None)
