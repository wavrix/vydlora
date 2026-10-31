"""Security helpers: safe path resolution + a tiny in-memory rate limiter."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock


def safe_join(base: Path, *parts: str) -> Path:
    """Join paths and guarantee the result stays inside `base` (no traversal)."""
    base = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if base != candidate and base not in candidate.parents:
        raise ValueError(f"Path escapes base directory: {candidate}")
    return candidate


class RateLimiter:
    """Sliding-window limiter keyed by an arbitrary string (e.g. client IP)."""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True
