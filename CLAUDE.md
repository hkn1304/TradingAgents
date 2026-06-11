# TradingAgents — Project Notes

## Critical gotchas

### yfinance `end` date is EXCLUSIVE
`yf.download(start=..., end=...)` and `provider.get_ohlcv(...)` drop every bar
after the `end` date boundary. Passing `end = utcnow().strftime("%Y-%m-%d")`
(today at midnight) silently excludes ALL of today's intraday bars, making
prices/signals stale for up to 24h with no error.

**Rule: always pass `end = utcnow() + timedelta(days=1)` when fetching OHLCV
up to "now".** Fixed in commit a4a5fb6 at five call sites (server.py chart/
pivots/kalman/portfolio_kalman, guardian.py signal-flip). Any new endpoint
that fetches OHLCV must follow the same rule.

### MT5 Python API quirks
- `history_deals_get()` silently returns nothing for timezone-aware datetimes —
  use naive UTC (`datetime.utcnow()`) or Unix int timestamps, and add a +1h
  buffer on `date_to`.
- Symbol names differ per broker: XM Global uses SILVER/GOLD, MetaQuotes demo
  uses XAGUSD/XAUUSD. `_xm_symbol()` in web/mt5_broker.py probes MT5 live and
  caches whichever name exists — never hardcode a symbol map lookup.

## Conventions
- MT5 credentials live in `.env` (gitignored): `MT5_LOGIN/PASSWORD/SERVER`
  (demo) and `MT5_REAL_LOGIN/PASSWORD/SERVER` (XM Global real).
- Server start: `uv run python api_server.py` (port 8000). Restarts sometimes
  need a retry with longer sleep (12-15s) before the port is listening.
- PWA service worker: never cache index.html; bump `CACHE` version in
  pwa/sw.js when changing cached static assets.
- yfinance downloads are wrapped in `_yf_download_lock` (threading lock) —
  stagger background fetches ~2s apart to avoid contention.
