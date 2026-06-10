"""
Position guardian: 60-second background loop that manages open MT5
positions after entry.

  · Breakeven lock — once price moves half the entry→SL distance in
    favour, the stop loss is moved to the entry price (worst case = 0).
  · Zombie exit   — positions stalled in loss beyond zombie_bars hours
    are closed; the signal has decayed.
  · Signal-flip alert — every 15 min the current Kalman direction is
    recomputed for each open position; a contradiction writes an alert.
  · Daily drawdown kill-switch — if equity falls drawdown_pct below the
    day's starting balance, auto-execution is disabled for the day.
  · Morning briefing — once per day at 08:00 UTC a plain-text summary
    of positions and watchlist signals is stored for the PWA banner.
  · Equity snapshot — one balance/equity row per day for the equity curve.

All MT5 / scan calls are blocking, so each tick runs in a thread
executor off the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from web.trades_db import (
    init_trades_db,
    trade_journal_close,
    trade_journal_open_tickets,
    equity_snapshot_insert,
    guardian_alert_insert,
    briefing_insert,
)

logger = logging.getLogger(__name__)

BRIEFING_HOUR_UTC = 8
FLIP_CHECK_INTERVAL_S = 900  # 15 min per ticker

# ── Module state (single guardian instance per process) ──────────────────────
_breakeven_done:  set[int] = set()           # tickets with SL already moved
_alerted_today:   set[tuple[int, str]] = set()  # (ticket, date) flip alerts sent
_last_flip_check: dict[str, float] = {}      # ticker → unix ts of last Kalman check
_last_seen:       dict[int, dict] = {}       # ticket → last known price/profit
_day_start_balance: Optional[float] = None
_current_day:       Optional[str] = None
_killswitch_fired_day: Optional[str] = None
_last_briefing_date:   Optional[str] = None
_last_snapshot_date:   Optional[str] = None


async def guardian_loop(broker, engine, scan_fn: Callable | None = None,
                        interval_s: int = 60):
    """
    broker  : MT5Broker singleton
    engine  : ExecutionEngine singleton
    scan_fn : callable(tickers_csv, horizon) -> portfolio scan dict,
              used for the morning briefing (passed in from server.py
              to avoid a circular import).
    """
    init_trades_db()
    logger.info("Guardian loop started")
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(interval_s)
        try:
            await loop.run_in_executor(None, _tick, broker, engine, scan_fn)
        except Exception as exc:
            logger.error(f"Guardian tick error: {exc}")


def _tick(broker, engine, scan_fn):
    _roll_day(broker)
    if not broker.connected:
        return
    cfg = engine.get_config()
    if not cfg.get("guardian_enabled", True):
        return
    positions = broker.get_positions()
    _sync_journal(positions)
    _check_positions(broker, engine, positions, cfg)
    _check_daily_drawdown(broker, engine, cfg)
    _maybe_morning_briefing(broker, engine, scan_fn)
    _maybe_equity_snapshot(broker)


def _roll_day(broker):
    """Reset per-day state at UTC midnight; record day-start balance."""
    global _current_day, _day_start_balance
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if today != _current_day:
        _current_day = today
        _day_start_balance = None
        _alerted_today.clear()
    if _day_start_balance is None and broker.connected:
        acct = broker.get_account_info()
        if acct:
            _day_start_balance = acct.balance


def _sync_journal(positions):
    """Close journal rows whose MT5 position has disappeared."""
    open_tickets = {p.ticket for p in positions}
    for p in positions:
        _last_seen[p.ticket] = {"price": p.current_price, "profit": p.profit}
    for ticket in trade_journal_open_tickets():
        if ticket not in open_tickets:
            seen = _last_seen.pop(ticket, {})
            trade_journal_close(ticket, seen.get("price"), seen.get("profit"))
            logger.info(f"Journal closed for ticket {ticket} "
                        f"pnl={seen.get('profit')}")


def _check_positions(broker, engine, positions, cfg):
    now_ts = datetime.utcnow().timestamp()
    zombie_h = cfg.get("zombie_bars", 20)

    for p in positions:
        # ── Breakeven lock ────────────────────────────────────────────────
        # The original SL sits 2×ATR away; half that distance ≈ 1×ATR of
        # favourable movement earns a free trade.
        if p.ticket not in _breakeven_done and p.sl:
            sl_dist = abs(p.entry_price - p.sl)
            be_mult = cfg.get("breakeven_atr_mult", 1.0)
            trigger = 0.5 * sl_dist * be_mult
            moved = (p.current_price - p.entry_price if p.direction == "buy"
                     else p.entry_price - p.current_price)
            already_locked = (p.sl >= p.entry_price if p.direction == "buy"
                              else p.sl <= p.entry_price)
            if already_locked:
                _breakeven_done.add(p.ticket)
            elif sl_dist > 0 and moved >= trigger:
                res = broker.modify_sl(p.ticket, p.entry_price)
                if res.success:
                    _breakeven_done.add(p.ticket)
                    logger.info(f"Breakeven lock: {p.symbol} ticket {p.ticket} "
                                f"SL → {p.entry_price}")

        # ── Zombie exit ───────────────────────────────────────────────────
        try:
            open_ts = float(p.open_time)
            age_h = (now_ts - open_ts) / 3600
        except (ValueError, TypeError):
            age_h = 0
        if age_h >= zombie_h and p.profit <= 0:
            res = broker.close_position(p.ticket)
            if res.success:
                trade_journal_close(p.ticket, p.current_price, p.profit)
                logger.info(f"Zombie exit: {p.symbol} ticket {p.ticket} "
                            f"after {age_h:.0f}h, pnl={p.profit}")
            continue

        # ── Signal-flip alert (rate-limited per ticker) ───────────────────
        last = _last_flip_check.get(p.symbol, 0)
        if now_ts - last >= FLIP_CHECK_INTERVAL_S:
            _last_flip_check[p.symbol] = now_ts
            _check_signal_flip(p)


def _check_signal_flip(position):
    """Recompute Kalman for the position's symbol; alert on contradiction."""
    key = (position.ticket, _current_day or "")
    if key in _alerted_today:
        return
    try:
        from datetime import timedelta
        from providers import get_market_provider, OHLCVInterval
        from web.kalman import compute_both

        provider = get_market_provider()
        end = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        df = provider.get_ohlcv(position.symbol, start, end, OHLCVInterval.HOUR_1)
        if df.empty or len(df) < 30:
            return
        both = compute_both(df, timeframe="1h")
        rw, cv = both["rw"], both["cv"]
        kalman_dir = rw.get("bias", "neutral")
        agree = rw.get("bias") == cv.get("bias")

        contradiction = (
            (position.direction == "buy" and kalman_dir == "bearish") or
            (position.direction == "sell" and kalman_dir == "bullish")
        )
        if contradiction and agree:
            guardian_alert_insert(
                ticker=position.symbol,
                ticket=position.ticket,
                position_dir=position.direction,
                kalman_dir=kalman_dir,
                score=0,
            )
            _alerted_today.add(key)
            logger.warning(f"Signal flip: {position.symbol} {position.direction} "
                           f"position vs Kalman {kalman_dir}")
    except Exception as exc:
        logger.debug(f"Flip check failed for {position.symbol}: {exc}")


def _check_daily_drawdown(broker, engine, cfg):
    global _killswitch_fired_day
    if _day_start_balance is None or _killswitch_fired_day == _current_day:
        return
    acct = broker.get_account_info()
    if acct is None or _day_start_balance <= 0:
        return
    dd = (acct.equity - _day_start_balance) / _day_start_balance
    limit = cfg.get("drawdown_pct", 0.02)
    if dd < -limit:
        engine.update_config(enabled=False)
        _killswitch_fired_day = _current_day
        logger.warning(f"Drawdown kill-switch fired: {dd:.2%} < -{limit:.2%} — "
                       "auto-execution disabled")


def _maybe_morning_briefing(broker, engine, scan_fn):
    global _last_briefing_date
    now = datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    if _last_briefing_date == today or now.hour < BRIEFING_HOUR_UTC:
        return
    _last_briefing_date = today

    lines = [f"Morning brief — {today}"]

    acct = broker.get_account_info()
    if acct:
        lines.append(f"Account: balance {acct.balance:.2f} {acct.currency}, "
                     f"floating P/L {acct.profit:+.2f}")

    positions = broker.get_positions()
    if positions:
        for p in positions:
            lines.append(f"{p.symbol} {p.direction.upper()} {p.volume} — "
                         f"P/L {p.profit:+.2f}")
    else:
        lines.append("No open positions.")

    cfg = engine.get_config()
    watch = cfg.get("auto_tickers", [])
    if watch and scan_fn:
        try:
            scan = scan_fn(",".join(watch), "1d")
            for r in scan.get("results", []):
                if r.get("error"):
                    continue
                lines.append(f"{r['ticker']}: {r['signal']} "
                             f"(score {r['signal_strength']}, {r['direction']})")
        except Exception as exc:
            logger.debug(f"Briefing scan failed: {exc}")

    if not cfg.get("enabled", True):
        lines.append("⚠ Auto-execution is DISABLED (kill-switch or manual).")

    briefing_insert(today, "\n".join(lines))
    logger.info("Morning briefing stored")


def _maybe_equity_snapshot(broker):
    global _last_snapshot_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _last_snapshot_date == today:
        return
    acct = broker.get_account_info()
    if acct is None:
        return
    equity_snapshot_insert(today, acct.balance, acct.equity, acct.profit)
    _last_snapshot_date = today
