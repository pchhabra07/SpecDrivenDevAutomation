"""
Shared approval-detection logic for Human Gates 1 and 2.

Both gates work identically: an MR is opened (or promoted to ready),
then the orchestrator polls the GitLab MR Approvals API until the
required approvals are present or a timeout is reached.

Using a single module for both gates avoids code duplication and
ensures consistent behaviour.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

from orchestrator.gitlab_client import GitLabClient

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
) -> GateResult:
    """
    Poll a GitLab MR or GitHub PR approval state until it is approved or times out.
    """
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

        if approvals.get("changes_requested") and return_on_changes_requested:
            comments = _fetch_comments(gitlab, mr_iid)
            logger.info("MR/PR #%d CHANGES_REQUESTED detected", mr_iid)
            return GateResult(
                outcome=GateOutcome.CHANGES_REQUESTED,
                approved_by=[],
                reviewer_comments=comments,
            )

        # Check for reviewer comments that might signal change requests.
        comments = _fetch_comments(gitlab, mr_iid)
        if comments:
            logger.info(
                "MR/PR #%d has %d reviewer comment(s) but is not yet approved.",
                mr_iid,
                len(comments),
            )
            if return_on_changes_requested:
                logger.info("Returning early on reviewer feedback for MR/PR #%d", mr_iid)
                return GateResult(
                    outcome=GateOutcome.CHANGES_REQUESTED,
                    approved_by=[],
                    reviewer_comments=comments,
                )

        logger.debug("MR/PR #%d not yet approved — polling again in %ds", mr_iid, poll_interval_seconds)
        time.sleep(poll_interval_seconds)

    raise GateTimeoutError(
        f"MR/PR #{mr_iid} was not approved within {timeout_hours}h"
    )


def _fetch_comments(gitlab: Any, mr_iid: int) -> list[str]:
    """Return non-system comment bodies from the MR or PR."""
    if hasattr(gitlab, "get_mr_notes"):
        notes = gitlab.get_mr_notes(mr_iid)
    elif hasattr(gitlab, "get_pr_notes"):
        notes = gitlab.get_pr_notes(mr_iid)
    else:
        notes = []
    return [n["body"] for n in notes if not n.get("system") and n.get("body")]
