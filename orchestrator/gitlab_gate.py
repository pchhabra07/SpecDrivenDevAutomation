"""
Unified approval-detection logic for Human Gates 1 and 2.

Both gates work identically across GitHub (Pull Requests) and GitLab (Merge Requests):
an MR or PR is opened (or promoted to ready), then the orchestrator polls the
approvals API until the required approvals are present or a timeout is reached.
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
    gitlab: Any,
    mr_iid: int,
    poll_interval_seconds: int = 30,
    timeout_hours: float = 48.0,
    return_on_changes_requested: bool = False,
    seen_comments: set[str] | None = None,
) -> GateResult:
    """
    Poll a GitLab MR or GitHub PR approval state until it is approved or times out.
    """
    if seen_comments is None:
        seen_comments = set()

    deadline = time.monotonic() + (timeout_hours * 3600)
    logger.info("Waiting for approval on MR/PR #%d (timeout=%.1fh)", mr_iid, timeout_hours)

    while time.monotonic() < deadline:
        if hasattr(gitlab, "get_mr_approvals"):
            approvals = gitlab.get_mr_approvals(mr_iid)
        elif hasattr(gitlab, "get_pr_approvals"):
            approvals = gitlab.get_pr_approvals(mr_iid)
        else:
            approvals = {}

        if approvals.get("approved"):
            comments = _fetch_comments(gitlab, mr_iid)
            result = GateResult(
                outcome=GateOutcome.APPROVED,
                approved_by=approvals.get("approved_by", []),
                reviewer_comments=comments,
            )
            logger.info("MR/PR #%d APPROVED by %s", mr_iid, result.approved_by)
            return result

        # Check for NEW reviewer comments that signal change requests
        all_comments = _fetch_comments(gitlab, mr_iid)
        new_comments = [c for c in all_comments if c not in seen_comments]

        if return_on_changes_requested and new_comments:
            logger.info(
                "MR/PR #%d has %d new reviewer comment(s). Returning early on feedback.",
                mr_iid,
                len(new_comments),
            )
            seen_comments.update(all_comments)
            return GateResult(
                outcome=GateOutcome.CHANGES_REQUESTED,
                approved_by=[],
                reviewer_comments=new_comments,
            )

        logger.debug("MR/PR #%d not yet approved — polling again in %ds", mr_iid, poll_interval_seconds)
        time.sleep(poll_interval_seconds)

    raise GateTimeoutError(
        f"MR/PR #{mr_iid} was not approved within {timeout_hours}h"
    )


def _fetch_comments(gitlab: Any, mr_iid: int) -> list[str]:
    """Return non-system reviewer comment bodies from the MR or PR."""
    if hasattr(gitlab, "get_mr_notes"):
        notes = gitlab.get_mr_notes(mr_iid)
    elif hasattr(gitlab, "get_pr_notes"):
        notes = gitlab.get_pr_notes(mr_iid)
    else:
        notes = []

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
