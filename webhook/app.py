"""
FastAPI Webhook Ingestion Service (Phase 3).

Receives incoming JIRA webhooks, authenticates them with HMAC-SHA256,
filters for qualifying tickets, deduplicates retransmissions, and enqueues
them for serial processing by the orchestrator worker.

Implements FR-1, FR-2, FR-27, and FR-28.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from orchestrator.config import get_settings
from orchestrator.main import run_pipeline

logger = logging.getLogger("webhook")

# In-process FIFO queue for serial ticket execution.
task_queue: asyncio.Queue[str] = asyncio.Queue()

# In-memory deduplication cache: {event_id: timestamp}
_recent_events: dict[str, float] = {}
_DEDUP_TTL_SECONDS = 600.0  # 10 minutes

from orchestrator.concurrency import ConcurrentWorkerPool

# Concurrent worker pool instance
worker_pool: ConcurrentWorkerPool | None = None


def _cleanup_old_dedup_entries() -> None:
    """Evict entries older than _DEDUP_TTL_SECONDS from deduplication cache."""
    now = time.time()
    expired = [k for k, ts in _recent_events.items() if now - ts > _DEDUP_TTL_SECONDS]
    for k in expired:
        del _recent_events[k]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager: launch the concurrent worker pool on startup."""
    global worker_pool
    settings = get_settings()
    worker_pool = ConcurrentWorkerPool(
        queue=task_queue,
        pipeline_runner=run_pipeline,
        max_workers=settings.max_concurrent_tickets,
    )
    worker_pool.start()
    logger.info("Webhook service initialized with %d workers.", settings.max_concurrent_tickets)
    yield
    if worker_pool:
        await worker_pool.stop()
    logger.info("Webhook service shut down.")
    logger.info("Webhook service shut down.")


app = FastAPI(
    title="Spec-Driven Development Webhook Service",
    description="Event-driven JIRA webhook receiver with serial task queueing.",
    version="1.0.0",
    lifespan=lifespan,
)


def verify_jira_hmac(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """
    Validate HMAC-SHA256 signature from JIRA webhook.
    Returns True if valid or if secret is unset; False if invalid.
    """
    if not secret:
        return True  # If no secret is configured, authentication is bypassed

    if not signature_header:
        return False

    # Signature may be 'sha256=<hex>' or just '<hex>'
    token = signature_header.removeprefix("sha256=").strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(token.lower(), expected.lower())


@app.post("/webhooks/jira", status_code=status.HTTP_202_ACCEPTED)
async def handle_jira_webhook(
    request: Request,
    x_hub_signature: str | None = Header(None, alias="X-Hub-Signature"),
    secret: str | None = Query(None),
) -> dict[str, Any]:
    """
    Receive JIRA webhook POST notifications for issue creation/update.
    """
    settings = get_settings()
    raw_body = await request.body()

    # 1. HMAC Verification
    sig = x_hub_signature or secret
    if settings.jira_webhook_secret and not verify_jira_hmac(raw_body, sig, settings.jira_webhook_secret):
        logger.warning("Rejected JIRA webhook — invalid HMAC signature.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid HMAC signature",
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    # 2. Extract key fields
    event_type = payload.get("webhookEvent", "")
    issue = payload.get("issue", {})
    fields = issue.get("fields", {})
    issue_key = issue.get("key", "")

    if not issue_key:
        logger.info("Ignoring webhook without issue key (event: %s)", event_type)
        return {"status": "ignored", "reason": "no_issue_key"}

    # 3. Filter by project key
    project = fields.get("project", {})
    project_key = project.get("key", "")
    if settings.jira_project_key and project_key != settings.jira_project_key:
        logger.debug("Ignoring ticket %s from non-target project %s", issue_key, project_key)
        return {"status": "ignored", "reason": "project_mismatch"}

    # 4. Filter by qualifying label
    labels = fields.get("labels", [])
    if settings.qualifying_label not in labels:
        logger.debug("Ticket %s does not carry qualifying label '%s'", issue_key, settings.qualifying_label)
        return {"status": "ignored", "reason": "missing_qualifying_label"}

    # 5. Deduplication check
    timestamp = payload.get("timestamp", time.time())
    dedup_key = f"{issue_key}:{timestamp}"
    _cleanup_old_dedup_entries()
    if dedup_key in _recent_events:
        logger.info("Ignoring duplicate webhook for %s (timestamp: %s)", issue_key, timestamp)
        return {"status": "duplicate_ignored", "ticket_id": issue_key}

    _recent_events[dedup_key] = time.time()

    # 6. Enqueue ticket for serial processing
    await task_queue.put(issue_key)
    queue_pos = task_queue.qsize()
    logger.info("Enqueued %s for processing (queue position: %d)", issue_key, queue_pos)

    return {
        "status": "queued",
        "ticket_id": issue_key,
        "queue_position": queue_pos,
    }


@app.post("/webhooks/gitlab", status_code=status.HTTP_200_OK)
async def handle_gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(None, alias="X-Gitlab-Token"),
) -> dict[str, str]:
    """
    GitLab webhook endpoint for MR updates and approval events.
    """
    settings = get_settings()
    if settings.gitlab_webhook_secret and x_gitlab_token != settings.gitlab_webhook_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid GitLab token")

    payload = await request.json()
    object_kind = payload.get("object_kind")
    logger.info("Received GitLab webhook: %s", object_kind)
    return {"status": "received"}


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness and readiness probe for container orchestration."""
    telemetry = worker_pool.telemetry if worker_pool else {
        "status": "idle",
        "active_tickets": [],
        "busy_workers": 0,
        "max_workers": 1,
        "processed_count": 0,
        "failed_count": 0,
    }
    return {
        "status": "ok",
        "worker": telemetry.get("status", "idle"),
        "busy_workers": telemetry.get("busy_workers", 0),
        "max_workers": telemetry.get("max_workers", 1),
        "active_tickets": telemetry.get("active_tickets", []),
        "queue_length": task_queue.qsize(),
        "tickets_processed": telemetry.get("processed_count", 0),
        "tickets_failed": telemetry.get("failed_count", 0),
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus-compatible plain text metrics."""
    telemetry = worker_pool.telemetry if worker_pool else {
        "busy_workers": 0,
        "max_workers": 1,
        "processed_count": 0,
        "failed_count": 0,
    }
    return (
        "# HELP spec_dev_queue_length Number of tickets currently waiting in queue\n"
        "# TYPE spec_dev_queue_length gauge\n"
        f"spec_dev_queue_length {task_queue.qsize()}\n\n"
        "# HELP spec_dev_tickets_processed_total Total tickets successfully completed\n"
        "# TYPE spec_dev_tickets_processed_total counter\n"
        f"spec_dev_tickets_processed_total {telemetry.get('processed_count', 0)}\n\n"
        "# HELP spec_dev_tickets_failed_total Total tickets that failed or escalated\n"
        "# TYPE spec_dev_tickets_failed_total counter\n"
        f"spec_dev_tickets_failed_total {telemetry.get('failed_count', 0)}\n\n"
        "# HELP spec_dev_busy_workers Number of workers actively running a pipeline\n"
        "# TYPE spec_dev_busy_workers gauge\n"
        f"spec_dev_busy_workers {telemetry.get('busy_workers', 0)}\n\n"
        "# HELP spec_dev_max_workers Configured max concurrent workers\n"
        "# TYPE spec_dev_max_workers gauge\n"
        f"spec_dev_max_workers {telemetry.get('max_workers', 1)}\n"
    )
