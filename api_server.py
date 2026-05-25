"""
TradingAgents Web Server — entry point.

Usage:
  uv run api_server.py              # development
  uv run uvicorn api_server:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 or share via ngrok:
  ngrok http 8000
"""

import uvicorn
from web.server import app  # noqa: F401 — re-exported for uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
