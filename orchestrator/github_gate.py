"""
Shared approval-detection logic for Human Gates 1 and 2 on GitHub Pull Requests.

Both gates work identically: a PR is opened (or promoted to ready-for-review),
then the orchestrator polls the GitHub PR reviews and comments until an
approval is detected, changes are requested, or a timeout is reached.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class GateOutcome(str, Enum):
    APPROVED = "approved"
    TIMEOUT = "timeout"
    CHANGES_REQUESTED = "changes_requested"


@dataclass
class GateResult:
    """Result of a human-approval gate check."""

    outcome: GateOutcome
    approved_by: list[str]
    reviewer_comments: list[str]


class GateTimeoutError(Exception):
    """Raised when a gate poll exceeds *timeout_hours*."""


def wait_for_approval(
    client: Any,  # GitHubClient (or compatible duck-typed client)
    pr_number: int,
    poll_interval_seconds: int = 30,
    timeout_hours: float = 48.0,
    return_on_changes_requested: bool = False,
    seen_comments: set[str] | None = None,
) -> GateResult:
    """
    Poll a GitHub PR's approval state until it is approved or times out.
    """
    if seen_comments is None:
        seen_comments = set()

    deadline = time.monotonic() + (timeout_hours * 3600)
    logger.info("Waiting for approval on PR #%d (timeout=%.1fh)", pr_number, timeout_hours)

    while time.monotonic() < deadline:
        approvals = client.get_pr_approvals(pr_number)

        if approvals.get("approved"):
            comments = _fetch_comments(client, pr_number)
            result = GateResult(
                outcome=GateOutcome.APPROVED,
                approved_by=approvals.get("approved_by", []),
                reviewer_comments=comments,
            )
            logger.info("PR #%d APPROVED by %s", pr_number, result.approved_by)
            return result

        # Check for NEW reviewer comments that signal change requests
        all_comments = _fetch_comments(client, pr_number)
        new_comments = [c for c in all_comments if c not in seen_comments]

        if return_on_changes_requested and new_comments:
            logger.info("Returning early on reviewer feedback for PR #%d", pr_number)
            seen_comments.update(all_comments)
            return GateResult(
                outcome=GateOutcome.CHANGES_REQUESTED,
                approved_by=[],
                reviewer_comments=new_comments,
            )

        logger.debug("PR #%d not yet approved — polling again in %ds", pr_number, poll_interval_seconds)
        time.sleep(poll_interval_seconds)

    raise GateTimeoutError(
        f"PR #{pr_number} was not approved within {timeout_hours}h"
    )


def _fetch_comments(client: Any, pr_number: int) -> list[str]:
    """Return non-system reviewer comment bodies from the PR."""
    notes = client.get_pr_notes(pr_number)
    comments: list[str] = []
    for n in notes:
        if n.get("system"):
            continue
        body = (n.get("body") or "").strip()
        if not body:
            continue
        if "<!-- spec-driven-bot -->" in body or body.startswith("🤖 **Spec-Driven Dev"):
            continue
        comments.append(body)
    return comments

