"""
Background job execution for the TradingAgents web backend.

Two execution paths:
  Full pipeline  (Tab 3) — one at a time via a FIFO queue + single worker thread
  Agent-only run (Tab 2) — runs immediately in its own thread, bypasses queue
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
from typing import Any

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.graph.signal_processing import SignalProcessor
from tradingagents.default_config import DEFAULT_CONFIG

from web.database import (
    agent_status_init,
    agent_status_update,
    report_upsert,
    session_set_status,
    session_set_queue_position,
)
from web.stream import manager as ws_manager

logger = logging.getLogger(__name__)

# ── Agent ordering constants (mirrors cli/main.py) ────────────────────────────

ANALYST_ORDER = ["market", "social", "news", "fundamentals"]

ANALYST_AGENT_NAMES: dict[str, str] = {
    "market":       "Market Analyst",
    "social":       "Social Analyst",
    "news":         "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}

ANALYST_REPORT_MAP: dict[str, str] = {
    "market":       "market_report",
    "social":       "sentiment_report",
    "news":         "news_report",
    "fundamentals": "fundamentals_report",
}

ALL_AGENTS = [
    # Analyst team (dynamic based on selection)
    "Market Analyst", "Social Analyst", "News Analyst", "Fundamentals Analyst",
    # Fixed teams
    "Bull Researcher", "Bear Researcher", "Research Manager",
    "Trader",
    "Aggressive Analyst", "Neutral Analyst", "Conservative Analyst",
    "Portfolio Manager",
]

# ── Config builder ─────────────────────────────────────────────────────────────

def build_graph_config(job_config: dict) -> dict:
    """Merge job-level config on top of DEFAULT_CONFIG."""
    cfg = DEFAULT_CONFIG.copy()
    cfg["max_debate_rounds"]      = job_config.get("research_depth", 1)
    cfg["max_risk_discuss_rounds"] = job_config.get("research_depth", 1)
    cfg["llm_provider"]            = job_config.get("llm_provider", "anthropic")
    cfg["quick_think_llm"]         = job_config.get("quick_llm", cfg["quick_think_llm"])
    cfg["deep_think_llm"]          = job_config.get("deep_llm",  cfg["deep_think_llm"])
    cfg["backend_url"]             = job_config.get("backend_url")
    cfg["output_language"]         = job_config.get("output_language", "English")
    cfg["google_thinking_level"]   = job_config.get("google_thinking_level")
    cfg["openai_reasoning_effort"] = job_config.get("openai_reasoning_effort")
    cfg["anthropic_effort"]        = job_config.get("anthropic_effort")
    cfg["checkpoint_enabled"]      = False
    return cfg


# ── Chunk processor ────────────────────────────────────────────────────────────

class _ChunkProcessor:
    """
    Translates raw LangGraph stream chunks into DB updates + WebSocket events.
    Mirrors the logic from cli/main.py but writes to SQLite instead of
    updating an in-memory MessageBuffer.
    """

    def __init__(self, session_id: str, selected_analysts: list[str]) -> None:
        self.session_id       = session_id
        self.selected         = [a.lower() for a in selected_analysts]
        self._report_cache: dict[str, str] = {}   # section → content seen so far
        self._agent_states: dict[str, str] = {}   # agent → last known status

    def _set_agent(self, name: str, status: str) -> None:
        if self._agent_states.get(name) == status:
            return
        self._agent_states[name] = status
        agent_status_update(self.session_id, name, status)
        ws_manager.broadcast_sync(self.session_id, {
            "type": "agent_update",
            "agent": name,
            "status": status,
        })

    def _set_report(self, section: str, content: str) -> None:
        if not content or content == self._report_cache.get(section):
            return
        self._report_cache[section] = content
        report_upsert(self.session_id, section, content)
        ws_manager.broadcast_sync(self.session_id, {
            "type": "report_update",
            "section": section,
            "content": content,
        })

    def process(self, chunk: dict[str, Any]) -> None:
        # ── Analyst reports ──────────────────────────────────────────────────
        found_active = False
        for key in ANALYST_ORDER:
            if key not in self.selected:
                continue
            report_key  = ANALYST_REPORT_MAP[key]
            agent_name  = ANALYST_AGENT_NAMES[key]
            if chunk.get(report_key):
                self._set_report(report_key, chunk[report_key])
            has_report = bool(self._report_cache.get(report_key))
            if has_report:
                self._set_agent(agent_name, "completed")
            elif not found_active:
                self._set_agent(agent_name, "in_progress")
                found_active = True

        # Transition to research team when all analysts done
        if not found_active and self.selected:
            if self._agent_states.get("Bull Researcher") in (None, "pending"):
                self._set_agent("Bull Researcher", "in_progress")

        # ── Investment debate (Research team) ────────────────────────────────
        debate = chunk.get("investment_debate_state") or {}
        if debate:
            bull  = (debate.get("bull_history")  or "").strip()
            bear  = (debate.get("bear_history")   or "").strip()
            judge = (debate.get("judge_decision") or "").strip()

            if bull:
                self._set_agent("Bull Researcher", "in_progress")
                self._set_report("investment_plan",
                                 f"### Bull Researcher Analysis\n{bull}")
            if bear:
                self._set_agent("Bear Researcher", "in_progress")
                self._set_report("investment_plan",
                                 f"### Bear Researcher Analysis\n{bear}")
            if judge:
                self._set_report("investment_plan",
                                 f"### Research Manager Decision\n{judge}")
                self._set_agent("Bull Researcher",    "completed")
                self._set_agent("Bear Researcher",    "completed")
                self._set_agent("Research Manager",   "completed")
                self._set_agent("Trader",             "in_progress")

        # ── Trader ───────────────────────────────────────────────────────────
        if chunk.get("trader_investment_plan"):
            self._set_report("trader_investment_plan", chunk["trader_investment_plan"])
            self._set_agent("Trader",            "completed")
            self._set_agent("Aggressive Analyst","in_progress")

        # ── Risk debate ───────────────────────────────────────────────────────
        risk = chunk.get("risk_debate_state") or {}
        if risk:
            agg  = (risk.get("aggressive_history")   or "").strip()
            con  = (risk.get("conservative_history") or "").strip()
            neu  = (risk.get("neutral_history")      or "").strip()
            judge = (risk.get("judge_decision")      or "").strip()

            if agg:
                self._set_agent("Aggressive Analyst", "in_progress")
                self._set_report("final_trade_decision",
                                 f"### Aggressive Analyst\n{agg}")
            if con:
                self._set_agent("Conservative Analyst", "in_progress")
                self._set_report("final_trade_decision",
                                 f"### Conservative Analyst\n{con}")
            if neu:
                self._set_agent("Neutral Analyst", "in_progress")
                self._set_report("final_trade_decision",
                                 f"### Neutral Analyst\n{neu}")
            if judge:
                self._set_report("final_trade_decision",
                                 f"### Portfolio Manager Decision\n{judge}")
                for a in ("Aggressive Analyst", "Conservative Analyst",
                          "Neutral Analyst", "Portfolio Manager"):
                    self._set_agent(a, "completed")

        # ── final_trade_decision (top-level field) ───────────────────────────
        if chunk.get("final_trade_decision"):
            self._set_report("final_trade_decision", chunk["final_trade_decision"])


# ── Core execution ─────────────────────────────────────────────────────────────

def _execute(session_id: str, ticker: str, analysis_date: str,
             selected_analysts: list[str], job_config: dict) -> None:
    """
    Run TradingAgentsGraph for this session.
    Called from both the full-pipeline worker and agent-only threads.
    """
    session_set_status(session_id, "running")
    ws_manager.broadcast_sync(session_id, {"type": "status", "status": "running"})

    cfg   = build_graph_config(job_config)
    graph = TradingAgentsGraph(selected_analysts, config=cfg, debug=False)

    # Initialise agent rows in DB
    active_analyst_names = [
        ANALYST_AGENT_NAMES[k] for k in selected_analysts
        if k in ANALYST_AGENT_NAMES
    ]
    fixed_agents = [
        "Bull Researcher", "Bear Researcher", "Research Manager",
        "Trader",
        "Aggressive Analyst", "Neutral Analyst", "Conservative Analyst",
        "Portfolio Manager",
    ]
    agent_status_init(session_id, active_analyst_names + fixed_agents)

    processor = _ChunkProcessor(session_id, selected_analysts)
    init_state = graph.propagator.create_initial_state(ticker, analysis_date)
    args = graph.propagator.get_graph_args()

    trace: list[dict] = []
    try:
        for chunk in graph.graph.stream(init_state, **args):
            processor.process(chunk)
            trace.append(chunk)
    except Exception as exc:
        logger.exception("Pipeline error for session %s", session_id)
        session_set_status(session_id, "failed", error=str(exc))
        ws_manager.broadcast_sync(session_id, {
            "type": "error", "message": str(exc),
        })
        return

    # Extract final rating
    final_rating = None
    if trace:
        last = trace[-1]
        decision_text = last.get("final_trade_decision", "")
        if decision_text:
            try:
                final_rating = SignalProcessor().process_signal(decision_text)
            except Exception:
                pass
        # Ensure all final report sections are written
        for section in (
            "market_report", "sentiment_report", "news_report",
            "fundamentals_report", "investment_plan",
            "trader_investment_plan", "final_trade_decision",
        ):
            if last.get(section):
                processor._set_report(section, last[section])

    session_set_status(session_id, "completed", final_rating=final_rating)
    ws_manager.broadcast_sync(session_id, {
        "type": "done",
        "session_id": session_id,
        "final_rating": final_rating,
    })
    logger.info("Session %s completed — rating: %s", session_id, final_rating)


# ── Full pipeline queue (Tab 3) ────────────────────────────────────────────────

_full_queue: queue.Queue = queue.Queue()
_queue_lock = threading.Lock()
_queue_order: list[str] = []   # session_ids in FIFO order for position tracking


def _broadcast_queue_positions() -> None:
    with _queue_lock:
        order = list(_queue_order)
    for pos, sid in enumerate(order, start=1):
        session_set_queue_position(sid, pos)
        ws_manager.broadcast_sync(sid, {
            "type": "queue_update",
            "position": pos,
            "total": len(order),
        })


def _full_pipeline_worker() -> None:
    """Single background thread — processes full-pipeline jobs one at a time."""
    while True:
        job = _full_queue.get()
        session_id      = job["session_id"]
        try:
            with _queue_lock:
                if session_id in _queue_order:
                    _queue_order.remove(session_id)
            _broadcast_queue_positions()

            _execute(
                session_id      = session_id,
                ticker          = job["ticker"],
                analysis_date   = job["analysis_date"],
                selected_analysts = job["selected_analysts"],
                job_config      = job["config"],
            )
        except Exception:
            logger.exception("Unhandled error in full pipeline worker")
        finally:
            _full_queue.task_done()


def start_worker() -> None:
    """Start the single full-pipeline worker thread. Called once at server startup."""
    t = threading.Thread(target=_full_pipeline_worker, daemon=True, name="pipeline-worker")
    t.start()
    logger.info("Full-pipeline worker thread started")


def enqueue_full_pipeline(
    session_id: str,
    ticker: str,
    analysis_date: str,
    selected_analysts: list[str],
    config: dict,
) -> int:
    """Add a full-pipeline job to the queue. Returns the queue position (1-based)."""
    with _queue_lock:
        _queue_order.append(session_id)
        position = len(_queue_order)

    session_set_queue_position(session_id, position)
    _full_queue.put({
        "session_id":        session_id,
        "ticker":            ticker,
        "analysis_date":     analysis_date,
        "selected_analysts": selected_analysts,
        "config":            config,
    })
    return position


# ── Agent-only run (Tab 2) ─────────────────────────────────────────────────────

def run_agents_immediate(
    session_id: str,
    ticker: str,
    analysis_date: str,
    selected_analysts: list[str],
    config: dict,
) -> None:
    """
    Launch selected agents in a dedicated thread — bypasses the queue.
    For Tier 1 (analyst-only) runs the full graph still executes, but
    the UI presents only the analyst-section outputs.
    """
    config_copy = dict(config)
    config_copy.setdefault("research_depth", 1)  # minimal downstream cost

    t = threading.Thread(
        target=_execute,
        kwargs=dict(
            session_id        = session_id,
            ticker            = ticker,
            analysis_date     = analysis_date,
            selected_analysts = selected_analysts,
            job_config        = config_copy,
        ),
        daemon=True,
        name=f"agent-run-{session_id[:8]}",
    )
    t.start()
