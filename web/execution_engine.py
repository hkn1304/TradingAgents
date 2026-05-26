"""
Execution engine: dual-confirmation gate between Kalman filter signals
and the agent pipeline's final trade decision.

A trade is submitted only when BOTH agree and clear minimum thresholds:

  Kalman : score >= min_kalman_score  AND  models_agree  AND
           direction in {bullish, bearish}

  Agent  : most recent completed session for the ticker (within
           agent_max_age_h) has final_trade_decision rating in
           {Buy, Overweight}  (for bullish)  or
           {Sell, Underweight} (for bearish)

Position sizing is ATR-based: risk_pct of account balance per trade,
with stop loss at 2 × ATR from entry.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy imports to avoid circular dependency at module load time
_parse_rating  = None
_session_list  = None
_report_get_all = None


def _load_deps():
    global _parse_rating, _session_list, _report_get_all
    if _parse_rating is None:
        from tradingagents.agents.utils.rating import parse_rating
        from web.database import session_list, report_get_all
        _parse_rating   = parse_rating
        _session_list   = session_list
        _report_get_all = report_get_all


BULLISH_RATINGS = {"Buy", "Overweight"}
BEARISH_RATINGS = {"Sell", "Underweight"}


# ── Config + signal dataclasses ───────────────────────────────────────────────

@dataclass
class ExecutionConfig:
    risk_pct:           float = 0.01   # fraction of account balance to risk per trade
    max_positions:      int   = 3      # hard cap on concurrent open positions
    min_kalman_score:   int   = 65     # minimum Kalman score required
    min_close_score:    int   = 70     # minimum score to trigger auto-close on reversal
    agent_max_age_h:    float = 24.0   # max age (hours) of accepted agent session
    auto_tickers:       set   = field(default_factory=set)  # tickers with auto=ON
    enabled:            bool  = True   # global kill-switch
    require_agent:      bool  = True   # when False, execute on Kalman signal alone
    auto_close:         bool  = False  # auto-close open position when signal reverses


@dataclass
class ExecutionSignal:
    ticker:       str
    direction:    str           # 'buy' | 'sell'
    kalman_score: int
    agent_rating: str
    session_id:   str
    entry_price:  Optional[float]
    stop_loss:    Optional[float]
    position_pct: Optional[float]
    age_hours:    float


# ── Engine ────────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Application-level singleton.  Thread-safe via _lock.
    Created once in server.py alongside the MT5Broker.
    """

    def __init__(self, broker):
        from web.mt5_broker import MT5Broker  # avoid circular at import time
        self._broker: MT5Broker = broker
        self._cfg   = ExecutionConfig()
        self._lock  = threading.Lock()
        self._log:  list[dict] = []   # last 100 execution attempts

    # ── Config ────────────────────────────────────────────────────────────────

    def update_config(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if k == 'auto_tickers':
                    self._cfg.auto_tickers = set(str(t).upper() for t in v)
                elif hasattr(self._cfg, k):
                    setattr(self._cfg, k, v)

    def get_config(self) -> dict:
        with self._lock:
            c = self._cfg
            return {
                "risk_pct":         c.risk_pct,
                "max_positions":    c.max_positions,
                "min_kalman_score": c.min_kalman_score,
                "min_close_score":  c.min_close_score,
                "agent_max_age_h":  c.agent_max_age_h,
                "auto_tickers":     sorted(c.auto_tickers),
                "enabled":          c.enabled,
                "require_agent":    c.require_agent,
                "auto_close":       c.auto_close,
            }

    # ── Concurrence check ─────────────────────────────────────────────────────

    def check_concurrence(
        self,
        ticker: str,
        kalman_result: dict,
    ) -> "tuple[Optional[ExecutionSignal], str]":
        """
        Return an ExecutionSignal if both Kalman and a recent agent session
        agree on direction, else None.
        """
        cfg          = self._cfg
        direction    = kalman_result.get('direction', 'neutral')
        score        = kalman_result.get('signal_strength', 0)
        models_agree = kalman_result.get('models_agree', False)

        logger.info(
            f"check_concurrence {ticker}: dir={direction} score={score} "
            f"agree={models_agree} min_score={cfg.min_kalman_score} "
            f"require_agent={cfg.require_agent} enabled={cfg.enabled}"
        )
        if not cfg.enabled:
            return None, "Engine disabled (kill-switch is OFF)"
        if direction == 'neutral':
            return None, f"Direction is neutral — no clear signal for {ticker}"
        if score < cfg.min_kalman_score:
            return None, f"Score {score} below minimum {cfg.min_kalman_score} — lower Min Kalman Score in Config"
        if cfg.require_agent and not models_agree:
            return None, "RW and CV models disagree — uncheck 'Require agent analysis' or wait for alignment"

        trade_dir = 'buy' if direction == 'bullish' else 'sell'

        if cfg.require_agent:
            agent = self._find_recent_agent_signal(ticker, cfg.agent_max_age_h)
            if agent is None:
                return None, f"No completed agent analysis for {ticker} within {cfg.agent_max_age_h}h — run Tab 2/3 first"
            rating = agent['rating']
            if direction == 'bullish' and rating not in BULLISH_RATINGS:
                return None, f"Agent rated {rating} but Kalman is bullish — signals conflict"
            if direction == 'bearish' and rating not in BEARISH_RATINGS:
                return None, f"Agent rated {rating} but Kalman is bearish — signals conflict"
            return ExecutionSignal(
                ticker       = ticker,
                direction    = trade_dir,
                kalman_score = score,
                agent_rating = rating,
                session_id   = agent['session_id'],
                entry_price  = agent.get('entry_price'),
                stop_loss    = agent.get('stop_loss'),
                position_pct = agent.get('position_pct'),
                age_hours    = agent['age_hours'],
            ), "OK"

        # Kalman-only mode — no agent analysis required
        return ExecutionSignal(
            ticker       = ticker,
            direction    = trade_dir,
            kalman_score = score,
            agent_rating = 'Kalman-only',
            session_id   = '',
            entry_price  = None,
            stop_loss    = None,
            position_pct = None,
            age_hours    = 0.0,
        ), "OK"

    def _find_recent_agent_signal(
        self, ticker: str, max_age_h: float
    ) -> Optional[dict]:
        """
        Scan completed sessions for ticker (most recent first).
        Return metadata for the first one within max_age_h, or None.
        """
        _load_deps()
        cutoff = datetime.utcnow() - timedelta(hours=max_age_h)

        try:
            sessions = _session_list(limit=100)
        except Exception as exc:
            logger.error(f"session_list error: {exc}")
            return None

        for sess in sessions:
            if sess.get('ticker', '').upper() != ticker.upper():
                continue
            if sess.get('status') != 'completed':
                continue

            ts_str = sess.get('completed_at') or sess.get('created_at', '')
            try:
                completed_at = datetime.fromisoformat(ts_str.replace('Z', ''))
                if completed_at < cutoff:
                    continue
                age_h = (datetime.utcnow() - completed_at).total_seconds() / 3600
            except Exception:
                continue

            try:
                reports = _report_get_all(sess['id'])
            except Exception:
                continue

            text = reports.get('final_trade_decision', '') \
                or reports.get('trader_investment_plan', '')
            if not text:
                continue

            rating = _parse_rating(text)
            # If the most recent completed session says Hold, stop looking
            if rating == 'Hold':
                return None

            entry, sl, pos_pct = _parse_trade_levels(
                reports.get('trader_investment_plan', '')
            )
            return {
                'session_id':  sess['id'],
                'rating':      rating,
                'entry_price': entry,
                'stop_loss':   sl,
                'position_pct': pos_pct,
                'age_hours':   round(age_h, 1),
            }

        return None

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute_signal(
        self,
        signal: ExecutionSignal,
        atr:    Optional[float] = None,
    ) -> dict:
        """
        Submit a market order for a confirmed signal.
        Returns a log-entry dict (success or failure).
        """
        with self._lock:
            if not self._broker.connected:
                return self._record(signal, False, "MT5 not connected")

            cfg = self._cfg

            # Guard: position already open for this ticker?
            existing = self._broker.get_position_for_symbol(signal.ticker)
            if existing:
                return self._record(signal, False,
                    f"Position already open — ticket {existing.ticket}")

            # Guard: max concurrent positions
            n_open = len(self._broker.get_positions())
            if n_open >= cfg.max_positions:
                return self._record(signal, False,
                    f"Max positions reached ({cfg.max_positions})")

            acct = self._broker.get_account_info()
            if acct is None:
                return self._record(signal, False, "Cannot read account info")

            # Stop loss: use agent's SL if available, else 2×ATR
            sl = signal.stop_loss
            if sl is None and atr is not None and signal.entry_price:
                dist = 2.0 * atr
                sl = (signal.entry_price - dist if signal.direction == 'buy'
                      else signal.entry_price + dist)

            # Volume: ATR-based risk sizing, fallback to minimum lot
            volume = 0.01
            if sl is not None and signal.entry_price:
                dist = abs(signal.entry_price - sl)
                if dist > 0:
                    risk_cash = acct.balance * cfg.risk_pct
                    volume    = risk_cash / dist
            volume = self._broker.normalize_volume(signal.ticker, volume)
            if volume <= 0:
                volume = 0.01

            result = self._broker.place_market_order(
                symbol    = signal.ticker,
                direction = signal.direction,
                volume    = volume,
                sl        = sl,
                comment   = f"TA-K{signal.kalman_score}",
            )
            return self._record(signal, result.success, result.comment,
                                ticket=result.ticket, volume=volume, sl=sl)

    def _record(
        self,
        signal:  ExecutionSignal,
        success: bool,
        comment: str,
        ticket:  Optional[int] = None,
        volume:  float = 0.0,
        sl:      Optional[float] = None,
    ) -> dict:
        entry = {
            "ts":           datetime.utcnow().isoformat(),
            "ticker":       signal.ticker,
            "direction":    signal.direction,
            "kalman_score": signal.kalman_score,
            "agent_rating": signal.agent_rating,
            "session_id":   signal.session_id,
            "age_hours":    signal.age_hours,
            "success":      success,
            "ticket":       ticket,
            "volume":       volume,
            "sl":           sl,
            "comment":      comment,
        }
        self._log.insert(0, entry)
        if len(self._log) > 100:
            self._log.pop()
        if success:
            logger.info(f"Executed {signal.ticker} {signal.direction} "
                        f"vol={volume} ticket={ticket}")
        else:
            logger.warning(f"Execution skipped {signal.ticker}: {comment}")
        return entry

    def get_log(self, limit: int = 50) -> list[dict]:
        return self._log[:limit]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_trade_levels(
    text: str,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Extract entry price, stop loss, and position sizing from
    a trader_investment_plan markdown string.
    Returns (entry_price, stop_loss, position_pct) — any may be None.
    """
    entry = stop = pos_pct = None
    for line in text.splitlines():
        ll = line.lower()
        m  = re.search(r'\$?([\d,]+(?:\.\d+)?)', line)
        if not m:
            continue
        try:
            val = float(m.group(1).replace(',', ''))
        except ValueError:
            continue
        if 'entry' in ll and entry is None:
            entry = val
        elif ('stop' in ll or 'sl' in ll) and stop is None:
            stop = val
        elif ('position' in ll or 'sizing' in ll or 'size' in ll) and pos_pct is None:
            if val <= 100:
                pos_pct = val
    return entry, stop, pos_pct
