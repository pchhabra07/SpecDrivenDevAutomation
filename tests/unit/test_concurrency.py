"""
Unit tests for orchestrator.concurrency (Phase 5).
"""

import asyncio
import time

import pytest

from orchestrator.concurrency import ConcurrentWorkerPool


@pytest.mark.asyncio
async def test_concurrent_worker_pool_execution():
    queue = asyncio.Queue()
    processed = []

    def dummy_runner(ticket_id: str) -> int:
        time.sleep(0.05)
        processed.append(ticket_id)
        return 0

    pool = ConcurrentWorkerPool(queue, dummy_runner, max_workers=2)
    pool.start()

    await queue.put("TICKET-1")
    await queue.put("TICKET-2")

    await queue.join()

    assert set(processed) == {"TICKET-1", "TICKET-2"}
    assert pool.telemetry["processed_count"] == 2
    assert pool.telemetry["failed_count"] == 0

    await pool.stop()


@pytest.mark.asyncio
async def test_concurrent_worker_pool_failure_isolation():
    queue = asyncio.Queue()

    def dummy_runner(ticket_id: str) -> int:
        if ticket_id == "FAIL-1":
            raise RuntimeError("Boom")
        return 0

    pool = ConcurrentWorkerPool(queue, dummy_runner, max_workers=2)
    pool.start()

    await queue.put("FAIL-1")
    await queue.put("OK-1")

    await queue.join()

    assert pool.telemetry["failed_count"] == 1
    assert pool.telemetry["processed_count"] == 1

    await pool.stop()


@pytest.mark.asyncio
async def test_concurrent_worker_pool_telemetry():
    queue = asyncio.Queue()

    def slow_runner(ticket_id: str) -> int:
        time.sleep(0.1)
        return 0

    pool = ConcurrentWorkerPool(queue, slow_runner, max_workers=2)
    pool.start()

    await queue.put("SLOW-1")

    # Give worker a moment to pick it up
    await asyncio.sleep(0.02)
    telem = pool.telemetry
    assert telem["max_workers"] == 2
    assert telem["status"] in ("busy", "idle")

    await queue.join()
    await pool.stop()
