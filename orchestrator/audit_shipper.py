"""
Audit Log Governance & SIEM Shipping (Phase 4).

Provides immutable audit trail persistence by exporting per-ticket JSONL
logs to either:
  1. A dedicated, write-restricted GitLab audit repository ('git' mode).
  2. An enterprise SIEM or HTTP log ingestion endpoint ('http' mode).

Implements FR-25.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.gitlab_client import GitLabClient

logger = logging.getLogger(__name__)


def compute_file_sha256(filepath: str | Path) -> str:
    """Compute the cryptographic SHA-256 digest of a local file."""
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"Audit file not found: {p}")
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def ship_audit_log_to_git(
    filepath: str | Path,
    gitlab: GitLabClient,
    audit_repo_id: int,
    branch: str = "main",
) -> str:
    """
    Commit the audit log to a dedicated audit repository in GitLab.

    The file is stored under `logs/YYYY/MM/<ticket_id>.jsonl` to ensure
    structured, immutable audit record keeping.
    """
    p = Path(filepath)
    content = p.read_text(encoding="utf-8")
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    ticket_id = p.stem  # e.g. "PROJ-123" from "PROJ-123.jsonl"

    now = datetime.datetime.now(datetime.timezone.utc)
    target_path = f"logs/{now.year:04d}/{now.month:02d}/{p.name}"
    commit_msg = (
        f"[audit] Ship audit log for {ticket_id}\n\n"
        f"SHA256: {sha256}\n"
        f"Timestamp: {now.isoformat()}"
    )

    # Use a project client targeting the audit repo
    audit_project = gitlab._gl.projects.get(audit_repo_id)

    # Determine commit action: create or update
    try:
        audit_project.files.get(file_path=target_path, ref=branch)
        action = "update"
    except Exception:
        action = "create"

    commit = audit_project.commits.create({
        "branch": branch,
        "commit_message": commit_msg,
        "actions": [{
            "action": action,
            "file_path": target_path,
            "content": content,
        }],
    })

    logger.info(
        "Shipped audit log for %s to GitLab repo %d (commit: %s, SHA256: %s)",
        ticket_id,
        audit_repo_id,
        commit.id[:10],
        sha256[:12],
    )
    return commit.id


def ship_audit_log_to_siem(
    filepath: str | Path,
    siem_url: str,
    api_token: str = "",
    timeout: float = 15.0,
) -> bool:
    """
    Stream the JSONL audit file entries to an HTTP SIEM endpoint.
    """
    p = Path(filepath)
    if not p.exists():
        logger.warning("Audit file %s does not exist — skipping SIEM shipping.", p)
        return False

    lines = [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        logger.info("Audit file %s is empty — nothing to ship.", p)
        return True

    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    try:
        resp = httpx.post(
            siem_url,
            json={"events": lines, "source": "spec-driven-automation"},
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code in (200, 201, 202):
            logger.info("Shipped %d audit events for %s to SIEM", len(lines), p.name)
            return True
        logger.warning("SIEM returned HTTP %d: %s", resp.status_code, resp.text)
        return False
    except httpx.HTTPError as exc:
        logger.error("Failed to ship audit log to SIEM: %s", exc)
        return False


def ship_audit_log(
    ticket_id: str,
    audit_dir: str,
    settings: Settings,
    gitlab: GitLabClient | None = None,
) -> dict[str, Any]:
    """
    High-level facade to ship a ticket's audit log according to configured mode.
    """
    filepath = Path(audit_dir) / f"{ticket_id}.jsonl"
    if not filepath.exists():
        logger.info("No audit log found for %s at %s", ticket_id, filepath)
        return {"status": "skipped", "reason": "file_not_found"}

    sha256 = compute_file_sha256(filepath)
    mode = settings.audit_shipping_mode.lower()

    if mode == "git":
        if not settings.audit_gitlab_repo_id:
            logger.warning("audit_shipping_mode is 'git' but audit_gitlab_repo_id is unset.")
            return {"status": "failed", "reason": "missing_audit_gitlab_repo_id", "sha256": sha256}
        if gitlab is None:
            logger.warning("GitLabClient not provided — cannot ship audit log via Git.")
            return {"status": "failed", "reason": "no_gitlab_client", "sha256": sha256}

        try:
            commit_id = ship_audit_log_to_git(
                filepath=filepath,
                gitlab=gitlab,
                audit_repo_id=settings.audit_gitlab_repo_id,
            )
            return {"status": "success", "mode": "git", "commit_id": commit_id, "sha256": sha256}
        except Exception as exc:
            logger.error("Git audit log shipping failed: %s", exc)
            return {"status": "failed", "mode": "git", "error": str(exc), "sha256": sha256}

    elif mode == "http":
        if not settings.siem_webhook_url:
            logger.warning("audit_shipping_mode is 'http' but siem_webhook_url is unset.")
            return {"status": "failed", "reason": "missing_siem_webhook_url", "sha256": sha256}

        success = ship_audit_log_to_siem(filepath=filepath, siem_url=settings.siem_webhook_url)
        return {
            "status": "success" if success else "failed",
            "mode": "http",
            "sha256": sha256,
        }

    else:
        logger.debug("Audit shipping mode is '%s' — local file retained.", mode)
        return {"status": "local_only", "sha256": sha256}
