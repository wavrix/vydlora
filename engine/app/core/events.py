"""WebSocket connection manager + thread-safe event publishing.

Workers run in background threads; the WebSocket broadcast must happen on the
asyncio event loop. `publish()` is safe to call from any thread - it schedules
the coroutine on the stored loop.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from .logging_config import get_logger

log = get_logger("events")


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[Any] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        log.debug("ws connected (%d total)", len(self._connections))

    async def disconnect(self, ws) -> None:
        async with self._lock:
            self._connections.discard(ws)
        log.debug("ws disconnected (%d total)", len(self._connections))

    @property
    def count(self) -> int:
        return len(self._connections)

    async def broadcast(self, message: dict) -> None:
        if not self._connections:
            return
        payload = json.dumps(message, default=str)
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)


manager = ConnectionManager()

# The main asyncio loop, captured at startup so worker threads can schedule on it.
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def publish(event_type: str, data: dict | None = None) -> None:
    """Thread-safe: broadcast an event to all connected WebSocket clients."""
    message = {
        "type": event_type,
        "data": data or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if _loop is None or not _loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(message), _loop)
    except Exception as exc:  # pragma: no cover
        log.debug("publish failed: %s", exc)
