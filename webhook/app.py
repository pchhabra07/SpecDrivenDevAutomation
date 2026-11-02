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
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
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


async def _stale_gate_monitor(poll_interval: float = 3600.0) -> None:
    """Periodically re-enqueue tickets whose gate wait has timed out (S5-1)."""
    while True:
        try:
            await asyncio.sleep(poll_interval)
            settings = get_settings()
            checkpoint_dir = Path(settings.checkpoint_dir)
            if not checkpoint_dir.exists():
                continue
            timeout_seconds = settings.gate_timeout_hours * 3600
            now = time.time()
            for fname in checkpoint_dir.glob("*.json"):
                try:
                    data = json.loads(fname.read_text(encoding="utf-8"))
                    if data.get("gate_waiting") and data.get("gate_waiting_since"):
                        elapsed = now - float(data["gate_waiting_since"])
                        if elapsed > timeout_seconds:
                            ticket_id = data.get("ticket_id")
                            if ticket_id:
                                logger.warning(
                                    "Gate timeout for ticket %s (gate=%s, elapsed=%.1fh). Re-enqueuing.",
                                    ticket_id,
                                    data["gate_waiting"],
                                    elapsed / 3600,
                                )
                                await task_queue.put(str(ticket_id))
                except Exception:
                    continue
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Error in stale-gate monitor loop: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager: launch the concurrent worker pool and stale-gate monitor on startup."""
    global worker_pool
    # Unconditionally silence low-level HTTP transport and wire dumps
    for noisy in (
        "urllib3",
        "urllib3.connectionpool",
        "requests",
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "openai",
        "openai._base_client",
        "github",
        "github.Requester",
        "git",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = get_settings()
    # In server/webhook mode, gates must not block worker threads (S2-1, S5-1)
    settings.blocking_gates = False
    worker_pool = ConcurrentWorkerPool(
        queue=task_queue,
        pipeline_runner=run_pipeline,
        max_workers=settings.max_concurrent_tickets,
    )
    worker_pool.start()
    logger.info("Webhook service initialized with %d workers.", settings.max_concurrent_tickets)
    stale_monitor_task = asyncio.create_task(_stale_gate_monitor())
    yield
    stale_monitor_task.cancel()
    try:
        await stale_monitor_task
    except asyncio.CancelledError:
        pass
    if worker_pool:
        await worker_pool.stop()
    logger.info("Webhook service shut down.")


app = FastAPI(
    title="Spec-Driven Development Webhook Service",
    description="Event-driven GitHub and JIRA webhook receiver with concurrent task queueing.",
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


def verify_github_hmac(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """
    Validate HMAC-SHA256 signature from GitHub webhook (X-Hub-Signature-256).
    Returns True if valid or if secret is unset; False if invalid.
    """
    if not secret:
        return True  # If no secret is configured, authentication is bypassed

    if not signature_header:
        return False

    token = signature_header.removeprefix("sha256=").strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(token.lower(), expected.lower())


@app.post("/webhooks/jira", status_code=status.HTTP_202_ACCEPTED)
async def handle_jira_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    secret: str | None = Query(None),
) -> dict[str, Any]:
    """
    Receive JIRA webhook POST notifications for issue creation/update.
    """
    settings = get_settings()
    raw_body = await request.body()

    # 1. HMAC Verification
    sig = x_hub_signature_256 or secret
    jira_secret = getattr(settings, "jira_webhook_secret", "")
    if jira_secret and not verify_jira_hmac(raw_body, sig, jira_secret):
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
    target_project_key = getattr(settings, "jira_project_key", "")
    if target_project_key and project_key != target_project_key:
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


def _find_ticket_for_pr(pr_number: int, checkpoint_dir: str) -> str | None:
    """Scan checkpoint files to find a ticket waiting on the given PR/MR number (S2-1)."""
    import json
    from pathlib import Path

    cp_dir = Path(checkpoint_dir)
    if not cp_dir.is_dir():
        return None

    for fname in cp_dir.glob("*.json"):
        try:
            data = json.loads(fname.read_text(encoding="utf-8"))
            if (
                data.get("gate_waiting_mr_iid") == pr_number
                or data.get("gate1_mr_iid") == pr_number
                or data.get("gate2_mr_iid") == pr_number
                or data.get("draft_mr_iid") == pr_number
            ):
                return str(data.get("ticket_id"))
        except Exception:
            continue
    return None


@app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def handle_github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
) -> dict[str, Any]:
    """
    Receive GitHub webhook POST notifications for issues or pull requests.
    """
    settings = get_settings()
    raw_body = await request.body()

    # 1. HMAC Verification
    secret = settings.github_webhook_secret
    if secret and not verify_github_hmac(raw_body, x_hub_signature_256, secret):
        logger.warning("Rejected GitHub webhook — invalid HMAC signature.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid HMAC signature",
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    # If ping event
    if x_github_event == "ping":
        return {"status": "pong"}

    # S2-1: Handle pull_request_review events (e.g. human approved or requested changes)
    if x_github_event == "pull_request_review":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        pr_number = pr.get("number")
        if action in ("submitted", "dismissed", "edited") and pr_number:
            review_id = payload.get("review", {}).get("id", "")
            dedup_key = f"github_review:{pr_number}:{action}:{review_id}"
            _cleanup_old_dedup_entries()
            if dedup_key in _recent_events:
                logger.info("Ignoring duplicate review webhook for PR #%s", pr_number)
                return {"status": "duplicate_ignored", "pr_number": pr_number}
            _recent_events[dedup_key] = time.time()

            ticket_id = _find_ticket_for_pr(pr_number, settings.checkpoint_dir)
            if ticket_id:
                await task_queue.put(ticket_id)
                queue_pos = task_queue.qsize()
                logger.info(
                    "Re-enqueued ticket %s after PR #%d review event (queue position: %d)",
                    ticket_id, pr_number, queue_pos,
                )
                return {
                    "status": "requeued",
                    "ticket_id": ticket_id,
                    "pr_number": pr_number,
                    "queue_position": queue_pos,
                }
            logger.info("No checkpoint found waiting for PR #%d", pr_number)
            return {"status": "ignored", "reason": "no_matching_checkpoint"}
        return {"status": "ignored", "reason": f"unhandled_review_action_{action}"}

    # S2-1: Handle issue_comment events on pull requests (e.g. reviewer commented on PR)
    if x_github_event == "issue_comment" and payload.get("issue", {}).get("pull_request"):
        action = payload.get("action", "")
        comment = payload.get("comment", {})
        body = comment.get("body", "")
        if "<!-- spec-driven-bot -->" in body or "[spec-driven]" in body:
            return {"status": "ignored", "reason": "bot_comment"}
        pr_number = payload.get("issue", {}).get("number")
        if action == "created" and pr_number:
            comment_id = comment.get("id", "")
            dedup_key = f"github_comment:{pr_number}:{comment_id}"
            _cleanup_old_dedup_entries()
            if dedup_key in _recent_events:
                return {"status": "duplicate_ignored", "pr_number": pr_number}
            _recent_events[dedup_key] = time.time()

            ticket_id = _find_ticket_for_pr(pr_number, settings.checkpoint_dir)
            if ticket_id:
                await task_queue.put(ticket_id)
                queue_pos = task_queue.qsize()
                logger.info(
                    "Re-enqueued ticket %s after PR #%d comment event (queue position: %d)",
                    ticket_id, pr_number, queue_pos,
                )
                return {
                    "status": "requeued",
                    "ticket_id": ticket_id,
                    "pr_number": pr_number,
                    "queue_position": queue_pos,
                }
            return {"status": "ignored", "reason": "no_matching_checkpoint"}

    # Filter for 'issues' event
    if x_github_event and x_github_event != "issues":
        logger.info("Ignoring GitHub event '%s'", x_github_event)
        return {"status": "ignored", "reason": f"unhandled_event_{x_github_event}"}

    action = payload.get("action", "")
    issue = payload.get("issue", {})
    if not issue:
        return {"status": "ignored", "reason": "no_issue"}

    issue_number = issue.get("number")
    if not issue_number:
        return {"status": "ignored", "reason": "no_issue_number"}

    ticket_id = str(issue_number)

    # Check repository if configured
    repository = payload.get("repository", {})
    repo_full_name = repository.get("full_name", "")
    if settings.github_repo and repo_full_name and settings.github_repo.lower() != repo_full_name.lower():
        logger.debug("Ignoring issue from non-target repo %s", repo_full_name)
        return {"status": "ignored", "reason": "repo_mismatch"}

    # Check qualifying label
    raw_labels = issue.get("labels", [])
    labels = [lbl.get("name") if isinstance(lbl, dict) else str(lbl) for lbl in raw_labels]
    if settings.qualifying_label and settings.qualifying_label not in labels:
        logger.debug("GitHub Issue #%s does not carry qualifying label '%s'", ticket_id, settings.qualifying_label)
        return {"status": "ignored", "reason": "missing_qualifying_label"}

    # Deduplication check
    updated_at = issue.get("updated_at") or payload.get("timestamp", time.time())
    dedup_key = f"github:{ticket_id}:{action}:{updated_at}"
    _cleanup_old_dedup_entries()
    if dedup_key in _recent_events:
        logger.info("Ignoring duplicate GitHub webhook for issue #%s", ticket_id)
        return {"status": "duplicate_ignored", "ticket_id": ticket_id}

    _recent_events[dedup_key] = time.time()

    # Enqueue issue number for processing
    await task_queue.put(ticket_id)
    queue_pos = task_queue.qsize()
    logger.info("Enqueued GitHub Issue #%s for processing (queue position: %d)", ticket_id, queue_pos)

    return {
        "status": "queued",
        "ticket_id": ticket_id,
        "queue_position": queue_pos,
    }


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
