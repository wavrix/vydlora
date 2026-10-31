"""Cross-thread nudge telling idle download workers a job is waiting.

Workers used to nap a fixed second between claim attempts, so a freshly queued
job sat untouched for up to that long before anything happened - the bulk of the
lag between pressing Download and the download actually starting.

Lives in core rather than the worker package so `repositories` can signal on
enqueue without importing the worker layer (which imports repositories).
"""

from __future__ import annotations

import threading

_job_waiting = threading.Event()


def notify() -> None:
    """Signal that a job was queued. Safe from any thread."""
    _job_waiting.set()


def wait(timeout: float) -> None:
    """Block until a job is queued or the timeout expires.

    Callers must `clear()` *before* claiming, never after: clearing afterwards
    would drop a signal raised while the claim was in flight.
    """
    _job_waiting.wait(timeout)


def clear() -> None:
    _job_waiting.clear()
