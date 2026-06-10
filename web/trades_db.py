"""
Standalone SQLite store for the guardian / self-calibration layer.

Separate from web.database so it can be added without touching the
existing schema: trade journal (signal snapshot at entry + outcome),
daily equity snapshots, guardian flip alerts, and morning briefings.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Optional

_DB_PATH = os.environ.get(
    "TRADES_DB_PATH",
    os.path.join(os.path.dirname(__file__), "trades.db"),
)
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_trades_db():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trade_journal (
            id           TEXT PRIMARY KEY,
            ticker       TEXT,
            direction    TEXT,
            volume       REAL,
            entry_price  REAL,
            exit_price   REAL,
            sl           REAL,
            mt5_ticket   INTEGER,
            opened_at    TEXT,
            closed_at    TEXT,
            pnl          REAL,
            won          INTEGER,
            kalman_score INTEGER,
            models_agree INTEGER,
            regime       TEXT,
            z_now        REAL,
            rw_signal    TEXT,
            cv_signal    TEXT,
            agent_rating TEXT,
            session_id   TEXT
        );
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            date    TEXT PRIMARY KEY,
            balance REAL,
            equity  REAL,
            profit  REAL
        );
        CREATE TABLE IF NOT EXISTS guardian_alerts (
            id           TEXT PRIMARY KEY,
            ts           TEXT,
            ticker       TEXT,
            ticket       INTEGER,
            position_dir TEXT,
            kalman_dir   TEXT,
            score        INTEGER,
            acknowledged INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS briefings (
            date TEXT PRIMARY KEY,
            text TEXT,
            ts   TEXT
        );
        """)


# ── Trade journal ─────────────────────────────────────────────────────────────

def trade_journal_insert(
    ticker: str,
    direction: str,
    volume: float,
    entry_price: Optional[float],
    sl: Optional[float],
    mt5_ticket: int,
    kalman_score: int = 0,
    models_agree: bool = False,
    regime: str = "",
    z_now: float = 0.0,
    rw_signal: str = "",
    cv_signal: str = "",
    agent_rating: str = "",
    session_id: str = "",
) -> str:
    jid = str(uuid.uuid4())
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO trade_journal
               (id, ticker, direction, volume, entry_price, sl, mt5_ticket,
                opened_at, kalman_score, models_agree, regime, z_now,
                rw_signal, cv_signal, agent_rating, session_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (jid, ticker, direction, volume, entry_price, sl, mt5_ticket,
             datetime.utcnow().isoformat(), kalman_score, int(models_agree),
             regime, z_now, rw_signal or "", cv_signal or "",
             agent_rating, session_id),
        )
    return jid


def trade_journal_close(
    mt5_ticket: int,
    exit_price: Optional[float],
    pnl: Optional[float],
) -> bool:
    won = None if pnl is None else int(pnl > 0)
    with _lock, _conn() as c:
        cur = c.execute(
            """UPDATE trade_journal
               SET exit_price=?, pnl=?, won=?, closed_at=?
               WHERE mt5_ticket=? AND closed_at IS NULL""",
            (exit_price, pnl, won, datetime.utcnow().isoformat(), mt5_ticket),
        )
        return cur.rowcount > 0


def trade_journal_open_tickets() -> list[int]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT mt5_ticket FROM trade_journal WHERE closed_at IS NULL"
        ).fetchall()
    return [r["mt5_ticket"] for r in rows]


def trade_journal_stats() -> dict:
    """Win rate / count / avg pnl by Kalman-score bucket and by regime."""
    with _lock, _conn() as c:
        rows = c.execute(
            """SELECT kalman_score, regime, pnl, won
               FROM trade_journal WHERE closed_at IS NOT NULL"""
        ).fetchall()

    buckets = {"40-55": [], "55-70": [], "70-85": [], "85-100": []}
    regimes = {"trending": [], "ranging": []}
    for r in rows:
        s = r["kalman_score"] or 0
        if   s >= 85: buckets["85-100"].append(r)
        elif s >= 70: buckets["70-85"].append(r)
        elif s >= 55: buckets["55-70"].append(r)
        elif s >= 40: buckets["40-55"].append(r)
        if r["regime"] in regimes:
            regimes[r["regime"]].append(r)

    def _agg(items):
        n = len(items)
        if n == 0:
            return {"trades": 0, "win_rate": None, "avg_pnl": None}
        wins = sum(1 for i in items if i["won"])
        pnls = [i["pnl"] for i in items if i["pnl"] is not None]
        return {
            "trades":   n,
            "win_rate": round(wins / n, 3),
            "avg_pnl":  round(sum(pnls) / len(pnls), 2) if pnls else None,
        }

    return {
        "total_closed": len(rows),
        "score_buckets": {k: _agg(v) for k, v in buckets.items()},
        "regimes":       {k: _agg(v) for k, v in regimes.items()},
    }


# ── Equity snapshots ──────────────────────────────────────────────────────────

def equity_snapshot_insert(date: str, balance: float, equity: float, profit: float):
    with _lock, _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO equity_snapshots (date, balance, equity, profit)
               VALUES (?,?,?,?)""",
            (date, balance, equity, profit),
        )


def equity_snapshot_list(limit: int = 90) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM equity_snapshots ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── Guardian alerts ───────────────────────────────────────────────────────────

def guardian_alert_insert(
    ticker: str, ticket: int, position_dir: str, kalman_dir: str, score: int
) -> str:
    aid = str(uuid.uuid4())
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO guardian_alerts
               (id, ts, ticker, ticket, position_dir, kalman_dir, score)
               VALUES (?,?,?,?,?,?,?)""",
            (aid, datetime.utcnow().isoformat(), ticker, ticket,
             position_dir, kalman_dir, score),
        )
    return aid


def guardian_alerts_list(limit: int = 20, unacked_only: bool = True) -> list[dict]:
    q = "SELECT * FROM guardian_alerts"
    if unacked_only:
        q += " WHERE acknowledged=0"
    q += " ORDER BY ts DESC LIMIT ?"
    with _lock, _conn() as c:
        rows = c.execute(q, (limit,)).fetchall()
    return [dict(r) for r in rows]


def guardian_alert_ack(alert_id: str) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE guardian_alerts SET acknowledged=1 WHERE id=?", (alert_id,)
        )
        return cur.rowcount > 0


# ── Briefings ─────────────────────────────────────────────────────────────────

def briefing_insert(date: str, text: str):
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO briefings (date, text, ts) VALUES (?,?,?)",
            (date, text, datetime.utcnow().isoformat()),
        )


def briefing_latest() -> Optional[dict]:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM briefings ORDER BY date DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
