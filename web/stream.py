"""WebSocket connection manager — broadcasts job progress to browser clients."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Event loop reference set during FastAPI lifespan startup.
# The job runner (background thread) uses this to schedule async broadcasts.
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


class ConnectionManager:
    """
    Tracks active WebSocket connections keyed by session_id.
    One session can have multiple browser tabs connected simultaneously.
    """

    def __init__(self) -> None:
        self._clients: dict[str, list["WebSocket"]] = {}

    async def connect(self, session_id: str, ws: "WebSocket") -> None:
        await ws.accept()
        self._clients.setdefault(session_id, []).append(ws)
        logger.debug("WS connected: session=%s total=%d",
                     session_id, len(self._clients[session_id]))

    def disconnect(self, session_id: str, ws: "WebSocket") -> None:
        clients = self._clients.get(session_id, [])
        if ws in clients:
            clients.remove(ws)
        if not clients:
            self._clients.pop(session_id, None)
        logger.debug("WS disconnected: session=%s", session_id)

    async def broadcast(self, session_id: str, payload: dict) -> None:
        """Send a JSON message to all clients watching this session."""
        clients = list(self._clients.get(session_id, []))
        if not clients:
            return
        text = json.dumps(payload)
        dead: list["WebSocket"] = []
        for ws in clients:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(session_id, ws)

    def broadcast_sync(self, session_id: str, payload: dict) -> None:
        """
        Thread-safe wrapper — schedules broadcast() on the FastAPI event loop.
        Called from the background job-runner thread.
        """
        if _loop is None or _loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self.broadcast(session_id, payload), _loop
        )


# Module-level singleton used by both server.py and job_runner.py
manager = ConnectionManager()
