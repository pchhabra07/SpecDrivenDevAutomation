"""
Concurrent Worker Pool & Task Scheduling (Phase 5).

Provides parallel execution of multiple JIRA tickets with shared rate-limit
budgeting, worker health telemetry, and graceful error isolation.

Implements Phase 5 concurrency scaling while respecting provider rate caps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ConcurrentWorkerPool:
    """
    Manages a pool of N asynchronous workers executing pipeline runs concurrently.
    """

    def __init__(
        self,
        queue: asyncio.Queue[str],
        pipeline_runner: Callable[[str], int],
        max_workers: int = 2,
    ) -> None:
        self.queue = queue
        self.pipeline_runner = pipeline_runner
        self.max_workers = max(1, max_workers)
        self.semaphore = asyncio.Semaphore(self.max_workers)

        self._worker_tasks: list[asyncio.Task] = []
        self._active_tickets: dict[int, str | None] = {i: None for i in range(self.max_workers)}
        self._processed_count = 0
        self._failed_count = 0
        self._is_running = False

    def start(self) -> None:
        """Launch the worker tasks."""
        if self._is_running:
            return
        self._is_running = True
        logger.info("Starting ConcurrentWorkerPool with %d workers.", self.max_workers)
        for worker_id in range(self.max_workers):
            task = asyncio.create_task(self._worker_loop(worker_id))
            self._worker_tasks.append(task)

    async def _worker_loop(self, worker_id: int) -> None:
        """Single worker loop consuming tickets from the queue."""
        logger.debug("Worker #%d started.", worker_id)
        while self._is_running:
            try:
                ticket_id = await self.queue.get()
            except asyncio.CancelledError:
                break

            self._active_tickets[worker_id] = ticket_id
            logger.info("Worker #%d acquired ticket %s.", worker_id, ticket_id)

            try:
                async with self.semaphore:
                    # Run the synchronous pipeline in a separate thread
                    result = await asyncio.to_thread(self.pipeline_runner, ticket_id)

                if result == 0:
                    self._processed_count += 1
                    logger.info("Worker #%d finished ticket %s (SUCCESS).", worker_id, ticket_id)
                else:
                    self._failed_count += 1
                    logger.warning("Worker #%d finished ticket %s (FAILURE/STATUS=%d).", worker_id, ticket_id, result)
            except Exception:
                self._failed_count += 1
                logger.exception("Worker #%d crashed while processing ticket %s.", worker_id, ticket_id)
            finally:
                self._active_tickets[worker_id] = None
                self.queue.task_done()

        logger.debug("Worker #%d terminated.", worker_id)

    async def stop(self) -> None:
        """Gracefully stop all worker tasks."""
        self._is_running = False
        for task in self._worker_tasks:
            task.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        logger.info("ConcurrentWorkerPool stopped.")

    @property
    def active_tickets(self) -> list[str]:
        """List of ticket IDs currently being processed across all workers."""
        return [t for t in self._active_tickets.values() if t is not None]

    @property
    def busy_worker_count(self) -> int:
        """Count of workers currently actively executing a pipeline."""
        return len(self.active_tickets)

    @property
    def telemetry(self) -> dict[str, Any]:
        """Telemetry snapshot for health/metrics endpoints."""
        return {
            "max_workers": self.max_workers,
            "busy_workers": self.busy_worker_count,
            "active_tickets": self.active_tickets,
            "processed_count": self._processed_count,
            "failed_count": self._failed_count,
            "queue_length": self.queue.qsize(),
            "status": "busy" if self.busy_worker_count > 0 else "idle",
        }
