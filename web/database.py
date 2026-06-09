"""SQLite persistence layer for the TradingAgents web backend."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "web_data" / "tradingagents.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Connection ─────────────────────────────────────────────────────────────────

@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _conn() as con:
        con.executescript("""
        -- One row per analysis run (full pipeline OR single-agent)
        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL DEFAULT 'default',
            ticker        TEXT NOT NULL,
            analysis_date TEXT NOT NULL,
            mode          TEXT NOT NULL DEFAULT 'full',   -- 'full' | 'agents'
            config_json   TEXT NOT NULL DEFAULT '{}',
            status        TEXT NOT NULL DEFAULT 'queued', -- queued|running|completed|failed
            queue_position INTEGER,
            final_rating  TEXT,
            created_at    TEXT NOT NULL,
            started_at    TEXT,
            completed_at  TEXT,
            error         TEXT
        );

        -- Report sections filled progressively as agents complete
        CREATE TABLE IF NOT EXISTS reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            section     TEXT NOT NULL,
            content     TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_reports ON reports(session_id, section);

        -- Per-agent status updated in real-time
        CREATE TABLE IF NOT EXISTS agent_status (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            agent_name   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            started_at   TEXT,
            completed_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_status ON agent_status(session_id, agent_name);

        -- Chat thread attached to a session (Tab 3 Q&A)
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role       TEXT NOT NULL,   -- 'user' | 'assistant'
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        -- Reusable analysis configurations
        CREATE TABLE IF NOT EXISTS templates (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL DEFAULT 'default',
            name        TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        -- Per-user LLM API key overrides (populated in release mode)
        CREATE TABLE IF NOT EXISTS user_keys (
            user_id  TEXT NOT NULL,
            provider TEXT NOT NULL,
            api_key  TEXT NOT NULL,
            PRIMARY KEY (user_id, provider)
        );

        CREATE TABLE IF NOT EXISTS kalman_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT    NOT NULL,
            timeframe   TEXT    NOT NULL,
            model       TEXT    NOT NULL,
            ts          TEXT    NOT NULL,
            price       REAL,
            filtered_price REAL,
            velocity    REAL,
            bias        TEXT,
            signal      TEXT,
            vel_signal  TEXT,
            z_score     REAL,
            gain        REAL,
            regime      TEXT,
            created_at  TEXT    DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ksig_ticker ON kalman_signals(ticker, timeframe, model, ts);

        -- Generic key-value store for user preferences (portfolio configs, etc.)
        CREATE TABLE IF NOT EXISTS user_prefs (
            key        TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)


# ── Sessions ───────────────────────────────────────────────────────────────────

def session_create(
    session_id: str,
    ticker: str,
    analysis_date: str,
    mode: str,
    config: dict,
    user_id: str = "default",
) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO sessions
               (id, user_id, ticker, analysis_date, mode, config_json,
                status, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (session_id, user_id, ticker, analysis_date, mode,
             json.dumps(config), "queued", _now()),
        )


def session_set_status(
    session_id: str,
    status: str,
    error: str | None = None,
    final_rating: str | None = None,
) -> None:
    now = _now()
    with _conn() as con:
        if status == "running":
            con.execute(
                "UPDATE sessions SET status=?, started_at=? WHERE id=?",
                (status, now, session_id),
            )
        elif status in ("completed", "failed"):
            con.execute(
                """UPDATE sessions SET status=?, completed_at=?,
                   error=?, final_rating=? WHERE id=?""",
                (status, now, error, final_rating, session_id),
            )
        else:
            con.execute(
                "UPDATE sessions SET status=? WHERE id=?",
                (status, session_id),
            )


def session_set_queue_position(session_id: str, pos: Optional[int]) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE sessions SET queue_position=? WHERE id=?",
            (pos, session_id),
        )


def _parse_session(row) -> dict:
    d = dict(row)
    d["config"] = json.loads(d.pop("config_json", "{}") or "{}")
    return d


def session_get(session_id: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    return _parse_session(row) if row else None


def session_list(user_id: str = "default", limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM sessions WHERE user_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [_parse_session(r) for r in rows]


def session_delete(session_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM sessions WHERE id=?", (session_id,))


# ── Reports ────────────────────────────────────────────────────────────────────

def report_upsert(session_id: str, section: str, content: str) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO reports (session_id, section, content, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(session_id, section)
               DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (session_id, section, content, _now()),
        )


def report_get_all(session_id: str) -> dict[str, str]:
    with _conn() as con:
        rows = con.execute(
            "SELECT section, content FROM reports WHERE session_id=?",
            (session_id,),
        ).fetchall()
    return {r["section"]: r["content"] for r in rows}


def report_get_full_text(session_id: str) -> str:
    """Concatenate all report sections into a single markdown string for chat context."""
    sections = report_get_all(session_id)
    _ORDER = [
        ("market_report",         "## Market Analysis"),
        ("sentiment_report",      "## Social Sentiment"),
        ("news_report",           "## News Analysis"),
        ("fundamentals_report",   "## Fundamentals Analysis"),
        ("investment_plan",       "## Research Team Decision"),
        ("trader_investment_plan","## Trading Team Plan"),
        ("final_trade_decision",  "## Portfolio Management Decision"),
    ]
    parts = []
    for key, title in _ORDER:
        if sections.get(key):
            parts.append(f"{title}\n\n{sections[key]}")
    return "\n\n---\n\n".join(parts)


# ── Agent status ───────────────────────────────────────────────────────────────

def agent_status_init(session_id: str, agent_names: list[str]) -> None:
    with _conn() as con:
        con.executemany(
            """INSERT OR IGNORE INTO agent_status
               (session_id, agent_name, status) VALUES (?,?,'pending')""",
            [(session_id, name) for name in agent_names],
        )


def agent_status_update(
    session_id: str,
    agent_name: str,
    status: str,
) -> None:
    now = _now()
    with _conn() as con:
        if status == "in_progress":
            con.execute(
                """UPDATE agent_status SET status=?, started_at=?
                   WHERE session_id=? AND agent_name=?""",
                (status, now, session_id, agent_name),
            )
        elif status == "completed":
            con.execute(
                """UPDATE agent_status SET status=?, completed_at=?
                   WHERE session_id=? AND agent_name=?""",
                (status, now, session_id, agent_name),
            )
        else:
            con.execute(
                """UPDATE agent_status SET status=?
                   WHERE session_id=? AND agent_name=?""",
                (status, session_id, agent_name),
            )


def agent_status_get_all(session_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM agent_status WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Chat messages ──────────────────────────────────────────────────────────────

def chat_append(session_id: str, role: str, content: str) -> dict:
    now = _now()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO chat_messages (session_id, role, content, created_at)
               VALUES (?,?,?,?)""",
            (session_id, role, content, now),
        )
        row_id = cur.lastrowid
    return {"id": row_id, "session_id": session_id,
            "role": role, "content": content, "created_at": now}


def chat_get_history(session_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT id, role, content, created_at FROM chat_messages
               WHERE session_id=? ORDER BY id""",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Templates ─────────────────────────────────────────────────────────────────

def template_create(
    template_id: str,
    name: str,
    ticker: str,
    config: dict,
    user_id: str = "default",
) -> None:
    now = _now()
    with _conn() as con:
        con.execute(
            """INSERT INTO templates (id, user_id, name, ticker, config_json,
               created_at, updated_at) VALUES (?,?,?,?,?,?,?)""",
            (template_id, user_id, name, ticker, json.dumps(config), now, now),
        )


def template_update(template_id: str, name: str, ticker: str, config: dict) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE templates SET name=?, ticker=?, config_json=?, updated_at=?
               WHERE id=?""",
            (name, ticker, json.dumps(config), _now(), template_id),
        )


def _parse_template(row) -> dict:
    d = dict(row)
    d["config"] = json.loads(d.pop("config_json", "{}") or "{}")
    return d


def template_get(template_id: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM templates WHERE id=?", (template_id,)
        ).fetchone()
    return _parse_template(row) if row else None


def template_list(user_id: str = "default") -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM templates WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_parse_template(r) for r in rows]


def template_delete(template_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM templates WHERE id=?", (template_id,))


# ── Kalman signals ─────────────────────────────────────────────────────────────

def kalman_signal_upsert(ticker: str, timeframe: str, model: str, ts: str,
                          price: float, filtered_price: float,
                          velocity, bias: str, signal, vel_signal,
                          z_score: float, gain: float, regime: str):
    """Insert or replace the latest signal snapshot for this ticker/timeframe/model."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO kalman_signals
              (ticker, timeframe, model, ts, price, filtered_price, velocity,
               bias, signal, vel_signal, z_score, gain, regime)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ticker, timeframe, model, ts, price, filtered_price,
              velocity, bias, signal, vel_signal, z_score, gain, regime))


def kalman_signal_history(ticker: str, timeframe: str, limit: int = 50):
    """Return recent signal rows for both models, newest first."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT model, ts, price, filtered_price, velocity,
                   bias, signal, vel_signal, z_score, gain, regime
            FROM kalman_signals
            WHERE ticker=? AND timeframe=?
            ORDER BY ts DESC LIMIT ?
        """, (ticker, timeframe, limit)).fetchall()
    cols = ['model','ts','price','filtered_price','velocity',
            'bias','signal','vel_signal','z_score','gain','regime']
    return [dict(zip(cols, r)) for r in rows]


# ── User preferences (key-value store) ────────────────────────────────────────

def pref_get(key: str):
    """Return the parsed JSON value for key, or None if not set."""
    with _conn() as con:
        row = con.execute(
            "SELECT value_json FROM user_prefs WHERE key=?", (key,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def pref_set(key: str, value) -> None:
    """Upsert a JSON-serialisable value under key."""
    with _conn() as con:
        con.execute(
            """INSERT INTO user_prefs (key, value_json, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                                              updated_at=excluded.updated_at""",
            (key, json.dumps(value), _now()),
        )
