"""Live backend log feed for the app's Terminal page.

Everything the backend logs is mirrored to the UI, which means this sits on the
hot path of a bulk download - a run that logs thousands of lines in a few
minutes across three worker threads. So the design is deliberately defensive:

  * `emit` does one bounded deque append and returns. No formatting, no I/O, no
    lock contention with the worker that called it. A logging call must never be
    the reason a download is slow.
  * A single background thread drains the buffer a few times a second and sends
    ONE batched frame. Publishing per record would schedule a coroutine per log
    line onto the event loop; batching turns thousands of frames into a handful.
  * The buffer is capped. Under a flood the oldest lines are dropped rather than
    the process growing without limit, and the UI is told how many it missed -
    losing old lines is always better than stalling the downloads that produce
    them.

The same ring buffer answers the Terminal's "what happened just before I opened
this" request, so history and live tail come from one place.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

# Roughly a few minutes of a busy bulk run. Large enough that opening the
# Terminal mid-download shows real context, small enough to stay cheap.
BUFFER_SIZE = 2000

# How often the drain thread sends a batch. 4/second reads as live to a person
# while collapsing a burst of hundreds of lines into one frame.
FLUSH_INTERVAL = 0.25

# Records below this never reach the UI. DEBUG is for diagnosing the app itself
# and would bury the events a person is actually watching for.
MIN_LEVEL = logging.INFO

_buffer: deque[dict] = deque(maxlen=BUFFER_SIZE)
_pending: deque[dict] = deque(maxlen=BUFFER_SIZE)
_lock = threading.Lock()
_dropped = 0
_thread: threading.Thread | None = None
_stop = threading.Event()
_seq = 0


class LiveLogHandler(logging.Handler):
    """Copies every qualifying record into the ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        global _dropped, _seq
        if record.levelno < MIN_LEVEL:
            return
        # Anything logged while publishing would recurse straight back here.
        if record.name in ("events", "logstream"):
            return
        try:
            message = record.getMessage()
        except Exception:
            return
        line = {
            "seq": 0,
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "source": record.name,
            "message": message[:2000],
        }
        with _lock:
            _seq += 1
            line["seq"] = _seq
            # `maxlen` silently discards the oldest; count that so the UI can
            # say a gap exists rather than quietly showing a false history.
            if len(_pending) == _pending.maxlen:
                _dropped += 1
            _pending.append(line)
            _buffer.append(line)


def recent(limit: int = 500, after: int = 0) -> list[dict]:
    """The last lines, oldest first.

    `after` returns only lines newer than that sequence number, which is what
    makes an external tail (the PowerShell console) cheap: it polls with the
    last id it saw and gets back only what it has not printed.
    """
    with _lock:
        lines = list(_buffer)
    if after:
        lines = [ln for ln in lines if ln["seq"] > after]
    return lines[-max(1, min(limit, BUFFER_SIZE)):]


def _drain() -> list[dict]:
    global _dropped
    with _lock:
        if not _pending:
            return []
        batch = list(_pending)
        _pending.clear()
        if _dropped:
            batch.insert(0, {
                "seq": batch[0]["seq"],
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": "WARNING",
                "source": "logstream",
                "message": f"{_dropped} log line(s) dropped: the backend outran the display",
            })
            _dropped = 0
        return batch


def _loop() -> None:
    from .events import publish

    while not _stop.wait(FLUSH_INTERVAL):
        try:
            batch = _drain()
            if batch:
                publish("log_lines", {"lines": batch})
        except Exception:
            # A broken feed must never take the process down, and logging the
            # failure here would feed itself.
            pass


def start() -> None:
    """Attach the handler and begin draining. Safe to call twice."""
    global _thread
    if _thread and _thread.is_alive():
        return
    root = logging.getLogger()
    if not any(isinstance(h, LiveLogHandler) for h in root.handlers):
        handler = LiveLogHandler()
        handler.setLevel(MIN_LEVEL)
        root.addHandler(handler)
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="log-stream", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    if _thread:
        _thread.join(timeout=2)
    _thread = None


def stats() -> dict[str, Any]:
    with _lock:
        return {"buffered": len(_buffer), "pending": len(_pending), "dropped": _dropped}
