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
) -> GateResult:
    """
    Poll a GitHub PR's approval state until it is approved or times out.

    Parameters
    ----------
    client:
        Initialised ``GitHubClient``.
    pr_number:
        The pull request number to poll.
    poll_interval_seconds:
        Seconds between successive API polls.
    timeout_hours:
        Maximum hours to wait before raising ``GateTimeoutError``.
    return_on_changes_requested:
        If True, return immediately with CHANGES_REQUESTED when reviewer comments
        or change requests are detected on an unapproved PR.

    Returns
    -------
    GateResult
        Contains outcome, approver usernames, and reviewer comments.
    """
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

        if approvals.get("changes_requested") and return_on_changes_requested:
            comments = _fetch_comments(client, pr_number)
            logger.info("PR #%d CHANGES_REQUESTED detected", pr_number)
            return GateResult(
                outcome=GateOutcome.CHANGES_REQUESTED,
                approved_by=[],
                reviewer_comments=comments,
            )

        # Also inspect comments for change-requests
        comments = _fetch_comments(client, pr_number)
        if comments and return_on_changes_requested:
            logger.info("Returning early on reviewer feedback for PR #%d", pr_number)
            return GateResult(
                outcome=GateOutcome.CHANGES_REQUESTED,
                approved_by=[],
                reviewer_comments=comments,
            )

        logger.debug("PR #%d not yet approved — polling again in %ds", pr_number, poll_interval_seconds)
        time.sleep(poll_interval_seconds)

    raise GateTimeoutError(
        f"PR #{pr_number} was not approved within {timeout_hours}h"
    )


def _fetch_comments(client: Any, pr_number: int) -> list[str]:
    """Return non-system comment bodies from the PR."""
    notes = client.get_pr_notes(pr_number)
    return [n["body"] for n in notes if not n.get("system") and n.get("body")]
