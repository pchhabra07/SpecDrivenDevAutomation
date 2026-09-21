"""
In-process sliding-window rate limiter for outgoing NIM API calls.

Maintains a deque of UTC timestamps.  Before every call, evicts entries
older than 60 seconds and blocks (sleeps) if the cap has been reached.
Thread-safe via a simple Lock, though the orchestrator is single-threaded
in Phase 1-3.

Implements FR-27, FR-28.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from threading import Lock

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60.0


class RateLimiter:
    """Sliding-window rate limiter capped at *max_calls* per 60 seconds."""

    def __init__(self, max_calls: int = 32) -> None:
        self._max_calls = max_calls
        self._timestamps: deque[float] = deque()
        self._lock = Lock()

    # ── Synchronous API ─────────────────────────────────────────────────

    def acquire(self) -> None:
        """Block until a call slot is available, then record the call."""
        with self._lock:
            self._evict_expired()
            while len(self._timestamps) >= self._max_calls:
                sleep_for = self._timestamps[0] + _WINDOW_SECONDS - time.monotonic()
                if sleep_for > 0:
                    logger.info(
                        "Rate limit reached (%d/%d). Sleeping %.1fs.",
                        len(self._timestamps),
                        self._max_calls,
                        sleep_for,
                    )
                    # Release lock while sleeping so other code can inspect state.
                    self._lock.release()
                    try:
                        time.sleep(sleep_for)
                    finally:
                        self._lock.acquire()
                self._evict_expired()
            self._timestamps.append(time.monotonic())

    # ── Async API (used by the orchestrator's async main loop) ──────────

    async def async_acquire(self) -> None:
        """Async variant — yields to the event loop instead of blocking."""
        self._evict_expired()
        while len(self._timestamps) >= self._max_calls:
            sleep_for = self._timestamps[0] + _WINDOW_SECONDS - time.monotonic()
            if sleep_for > 0:
                logger.info(
                    "Rate limit reached (%d/%d). Async-sleeping %.1fs.",
                    len(self._timestamps),
                    self._max_calls,
                    sleep_for,
                )
                await asyncio.sleep(sleep_for)
            self._evict_expired()
        self._timestamps.append(time.monotonic())

    # ── Internals ───────────────────────────────────────────────────────

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - _WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    @property
    def current_count(self) -> int:
        """Number of calls in the current 60-second window (for tests)."""
        self._evict_expired()
        return len(self._timestamps)
