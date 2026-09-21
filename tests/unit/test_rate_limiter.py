"""
Unit tests for orchestrator.rate_limiter.
"""

import time
from unittest.mock import patch

import pytest

from orchestrator.rate_limiter import RateLimiter


class TestRateLimiter:
    """Tests for the sliding-window rate limiter."""

    def test_allows_calls_under_cap(self):
        """Calls below the cap should proceed without sleeping."""
        rl = RateLimiter(max_calls=5)
        for _ in range(5):
            rl.acquire()
        assert rl.current_count == 5

    def test_blocks_when_cap_reached(self):
        """When the cap is reached, acquire() should sleep until a slot opens."""
        rl = RateLimiter(max_calls=2)

        # Fill the window.
        rl.acquire()
        rl.acquire()
        assert rl.current_count == 2

        # Manually age the oldest entry so it expires quickly.
        rl._timestamps[0] -= 59.5  # will expire in ~0.5s

        start = time.monotonic()
        rl.acquire()  # should sleep briefly then succeed
        elapsed = time.monotonic() - start

        assert rl.current_count == 2  # one evicted, one new
        # Should have slept but not for more than 2s.
        assert elapsed < 2.0

    def test_evicts_expired_entries(self):
        """Entries older than 60s should be evicted automatically."""
        rl = RateLimiter(max_calls=10)
        rl.acquire()
        # Simulate the entry being 61 seconds old.
        rl._timestamps[0] -= 61
        assert rl.current_count == 0  # evicted

    def test_current_count_is_accurate(self):
        """current_count should reflect only non-expired entries."""
        rl = RateLimiter(max_calls=100)
        for _ in range(10):
            rl.acquire()
        assert rl.current_count == 10

    def test_cap_of_one(self):
        """Edge case: cap=1 should serialize all calls."""
        rl = RateLimiter(max_calls=1)
        rl.acquire()
        # Age it out.
        rl._timestamps[0] -= 61
        rl.acquire()
        assert rl.current_count == 1


class TestRateLimiterAsync:
    """Tests for the async variant."""

    @pytest.mark.asyncio
    async def test_async_acquire_under_cap(self):
        rl = RateLimiter(max_calls=5)
        for _ in range(5):
            await rl.async_acquire()
        assert rl.current_count == 5
