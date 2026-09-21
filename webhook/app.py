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

# Worker state for health monitoring
_worker_state = {
    "status": "idle",  # "idle" | "busy"
    "active_ticket": None,
    "tickets_processed": 0,
    "tickets_failed": 0,
    "last_processed_time": None,
}


def _cleanup_old_dedup_entries() -> None:
    """Evict entries older than _DEDUP_TTL_SECONDS from deduplication cache."""
    now = time.time()
    expired = [k for k, ts in _recent_events.items() if now - ts > _DEDUP_TTL_SECONDS]
    for k in expired:
        del _recent_events[k]


async def serial_worker() -> None:
    """
    Background worker that continuously pulls tickets from task_queue and
    processes them one at a time (serial execution).
    """
    logger.info("Serial task worker started.")
    while True:
        ticket_id = await task_queue.get()
        _worker_state["status"] = "busy"
        _worker_state["active_ticket"] = ticket_id
        logger.info("Worker picked up ticket %s from queue.", ticket_id)

        try:
            # Run the synchronous orchestrator pipeline in a thread pool
            result = await asyncio.to_thread(run_pipeline, ticket_id)
            if result == 0:
                _worker_state["tickets_processed"] += 1
                logger.info("Pipeline completed successfully for %s", ticket_id)
            else:
                _worker_state["tickets_failed"] += 1
                logger.warning("Pipeline exited with status %d for %s", result, ticket_id)
        except Exception:
            _worker_state["tickets_failed"] += 1
            logger.exception("Unexpected error processing %s in background worker", ticket_id)
        finally:
            _worker_state["status"] = "idle"
            _worker_state["active_ticket"] = None
            _worker_state["last_processed_time"] = time.time()
            task_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager: launch the background serial worker on startup."""
    worker_task = asyncio.create_task(serial_worker())
    logger.info("Webhook service initialized.")
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
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
    return {
        "status": "ok",
        "worker": _worker_state["status"],
        "active_ticket": _worker_state["active_ticket"],
        "queue_length": task_queue.qsize(),
        "tickets_processed": _worker_state["tickets_processed"],
        "tickets_failed": _worker_state["tickets_failed"],
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus-compatible plain text metrics."""
    return (
        "# HELP spec_dev_queue_length Number of tickets currently waiting in queue\n"
        "# TYPE spec_dev_queue_length gauge\n"
        f"spec_dev_queue_length {task_queue.qsize()}\n\n"
        "# HELP spec_dev_tickets_processed_total Total tickets successfully completed\n"
        "# TYPE spec_dev_tickets_processed_total counter\n"
        f"spec_dev_tickets_processed_total {_worker_state['tickets_processed']}\n\n"
        "# HELP spec_dev_tickets_failed_total Total tickets that failed or escalated\n"
        "# TYPE spec_dev_tickets_failed_total counter\n"
        f"spec_dev_tickets_failed_total {_worker_state['tickets_failed']}\n\n"
        "# HELP spec_dev_worker_busy Indicator if worker is actively running a pipeline\n"
        "# TYPE spec_dev_worker_busy gauge\n"
        f"spec_dev_worker_busy {1 if _worker_state['status'] == 'busy' else 0}\n"
    )
