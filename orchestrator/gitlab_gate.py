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
    gitlab: GitLabClient,
    mr_iid: int,
    poll_interval_seconds: int = 30,
    timeout_hours: float = 48.0,
) -> GateResult:
    """
    Poll a GitLab MR's approval state until it is approved or times out.

    Parameters
    ----------
    gitlab:
        Initialised ``GitLabClient``.
    mr_iid:
        The merge-request internal ID to poll.
    poll_interval_seconds:
        Seconds between successive API polls.
    timeout_hours:
        Maximum hours to wait before raising ``GateTimeoutError``.

    Returns
    -------
    GateResult
        Contains outcome, approver usernames, and any non-system comments
        posted since the MR was opened (useful for routing reviewer
        feedback back to the LLM).
    """
    deadline = time.monotonic() + (timeout_hours * 3600)
    logger.info("Waiting for approval on MR !%d (timeout=%.1fh)", mr_iid, timeout_hours)

    while time.monotonic() < deadline:
        approvals = gitlab.get_mr_approvals(mr_iid)

        if approvals.get("approved"):
            comments = _fetch_comments(gitlab, mr_iid)
            result = GateResult(
                outcome=GateOutcome.APPROVED,
                approved_by=approvals.get("approved_by", []),
                reviewer_comments=comments,
            )
            logger.info("MR !%d APPROVED by %s", mr_iid, result.approved_by)
            return result

        # Check for reviewer comments that might signal change requests
        # (heuristic: non-system comments posted after the MR creation).
        comments = _fetch_comments(gitlab, mr_iid)
        if comments:
            logger.info(
                "MR !%d has %d reviewer comment(s) but is not yet approved.",
                mr_iid,
                len(comments),
            )

        logger.debug("MR !%d not yet approved — polling again in %ds", mr_iid, poll_interval_seconds)
        time.sleep(poll_interval_seconds)

    raise GateTimeoutError(
        f"MR !{mr_iid} was not approved within {timeout_hours}h"
    )


def _fetch_comments(gitlab: GitLabClient, mr_iid: int) -> list[str]:
    """Return non-system comment bodies from the MR."""
    notes = gitlab.get_mr_notes(mr_iid)
    return [n["body"] for n in notes if not n.get("system")]
