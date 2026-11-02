"""
Unified approval-detection logic for Human Gates 1 and 2.

Both gates work identically across GitHub (Pull Requests) and GitLab (Merge Requests):
an MR or PR is opened (or promoted to ready), then the orchestrator polls the
approvals API until the required approvals are present, changes are requested, or a timeout is reached.
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
    PENDING = "pending"


@dataclass
class GateResult:
    """Result of a human-approval gate check."""

    outcome: GateOutcome
    approved_by: list[str]
    reviewer_comments: list[str]


class GateTimeoutError(Exception):
    """Raised when a gate poll exceeds *timeout_hours*."""


_APPROVAL_KEYWORDS = {"/approve", "approve", "approved", "lgtm", ":shipit:", "looks good to me"}


def _get_approvals(vcs: Any, target_id: int) -> dict[str, Any]:
    """Safely fetch approval dictionary from GitHub PR or GitLab MR."""
    if hasattr(vcs, "get_pr_approvals"):
        res = vcs.get_pr_approvals(target_id)
        if isinstance(res, dict):
            return res
    if hasattr(vcs, "get_mr_approvals"):
        res = vcs.get_mr_approvals(target_id)
        if isinstance(res, dict):
            return res
    return {}


def check_approval_once(
    gitlab: Any = None,
    mr_iid: int = 0,
    seen_comments: set[str] | None = None,
    *,
    client: Any = None,
    pr_number: int | None = None,
) -> GateResult:
    """
    Non-blocking single check of an MR/PR's approval state (S2-1).

    Returns
    -------
    GateResult
        - outcome=APPROVED if required approvals are present.
        - outcome=CHANGES_REQUESTED if reviewer requested changes or left new comments.
        - outcome=PENDING if no review decision has been made yet.
    """
    vcs = client if client is not None else gitlab
    target_id = pr_number if pr_number is not None else mr_iid

    if seen_comments is None:
        seen_comments = set()

    try:
        approvals = _get_approvals(vcs, target_id)
    except Exception as exc:
        logger.warning("Gate check failed for MR/PR #%d: %s", target_id, exc)
        return GateResult(outcome=GateOutcome.PENDING, approved_by=[], reviewer_comments=[])

    if approvals.get("approved"):
        comments = _fetch_comments(vcs, target_id)
        return GateResult(
            outcome=GateOutcome.APPROVED,
            approved_by=approvals.get("approved_by", []),
            reviewer_comments=comments,
        )

    # Check for explicit CHANGES_REQUESTED review state
    if approvals.get("changes_requested"):
        all_comments = _fetch_comments(vcs, target_id)
        new_comments = [c for c in all_comments if c not in seen_comments]
        seen_comments.update(all_comments)
        return GateResult(
            outcome=GateOutcome.CHANGES_REQUESTED,
            approved_by=[],
            reviewer_comments=new_comments or ["Reviewer requested changes on pull request."],
        )

    # Check for NEW reviewer comments (approval commands or change requests)
    all_comments = _fetch_comments(vcs, target_id)
    new_comments = [c for c in all_comments if c not in seen_comments]
    if new_comments:
        seen_comments.update(all_comments)
        for c in new_comments:
            clean = c.strip().lower()
            if clean in _APPROVAL_KEYWORDS or clean.startswith("/approve"):
                logger.info("MR/PR #%d APPROVED via comment: %s", target_id, c.strip())
                return GateResult(
                    outcome=GateOutcome.APPROVED,
                    approved_by=["reviewer-comment"],
                    reviewer_comments=new_comments,
                )
        return GateResult(
            outcome=GateOutcome.CHANGES_REQUESTED,
            approved_by=[],
            reviewer_comments=new_comments,
        )

    return GateResult(outcome=GateOutcome.PENDING, approved_by=[], reviewer_comments=[])


def wait_for_approval(
    gitlab: Any = None,
    mr_iid: int = 0,
    poll_interval_seconds: int = 30,
    timeout_hours: float = 48.0,
    return_on_changes_requested: bool = False,
    seen_comments: set[str] | None = None,
    *,
    client: Any = None,
    pr_number: int | None = None,
) -> GateResult:
    """
    Poll a GitLab MR or GitHub PR approval state until it is approved, changes are requested, or it times out.
    """
    vcs = client if client is not None else gitlab
    target_id = pr_number if pr_number is not None else mr_iid

    if seen_comments is None:
        seen_comments = set()

    deadline = time.monotonic() + (timeout_hours * 3600)
    logger.info("Waiting for approval on MR/PR #%d (timeout=%.1fh)", target_id, timeout_hours)

    while time.monotonic() < deadline:
        approvals = _get_approvals(vcs, target_id)

        if approvals.get("approved"):
            comments = _fetch_comments(vcs, target_id)
            result = GateResult(
                outcome=GateOutcome.APPROVED,
                approved_by=approvals.get("approved_by", []),
                reviewer_comments=comments,
            )
            logger.info("MR/PR #%d APPROVED by %s", target_id, result.approved_by)
            return result

        # Check for explicit CHANGES_REQUESTED review state
        if return_on_changes_requested and approvals.get("changes_requested"):
            all_comments = _fetch_comments(vcs, target_id)
            new_comments = [c for c in all_comments if c not in seen_comments]
            seen_comments.update(all_comments)
            logger.info("MR/PR #%d has explicit CHANGES_REQUESTED review state", target_id)
            return GateResult(
                outcome=GateOutcome.CHANGES_REQUESTED,
                approved_by=[],
                reviewer_comments=new_comments or ["Reviewer requested changes on pull request."],
            )

        # Check for NEW reviewer comments (approval commands or change requests)
        all_comments = _fetch_comments(vcs, target_id)
        new_comments = [c for c in all_comments if c not in seen_comments]

        if new_comments:
            seen_comments.update(all_comments)
            for c in new_comments:
                clean = c.strip().lower()
                if clean in _APPROVAL_KEYWORDS or clean.startswith("/approve"):
                    logger.info("MR/PR #%d APPROVED via comment keyword: %s", target_id, c.strip())
                    return GateResult(
                        outcome=GateOutcome.APPROVED,
                        approved_by=["reviewer-comment"],
                        reviewer_comments=new_comments,
                    )

            if return_on_changes_requested:
                logger.info(
                    "MR/PR #%d has %d new reviewer comment(s). Returning early on feedback.",
                    target_id,
                    len(new_comments),
                )
                return GateResult(
                    outcome=GateOutcome.CHANGES_REQUESTED,
                    approved_by=[],
                    reviewer_comments=new_comments,
                )

        logger.debug("MR/PR #%d not yet approved — polling again in %ds", target_id, poll_interval_seconds)
        time.sleep(poll_interval_seconds)

    raise GateTimeoutError(
        f"MR/PR #{target_id} was not approved within {timeout_hours}h"
    )


def _fetch_comments(gitlab: Any, mr_iid: int) -> list[str]:
    """Return non-system reviewer comment bodies from the MR or PR."""
    notes: list[dict[str, Any]] = []
    if hasattr(gitlab, "get_pr_notes"):
        res = gitlab.get_pr_notes(mr_iid)
        if isinstance(res, list):
            notes = res
    if not notes and hasattr(gitlab, "get_mr_notes"):
        res = gitlab.get_mr_notes(mr_iid)
        if isinstance(res, list):
            notes = res

    comments: list[str] = []
    for n in notes:
        if n.get("system"):
            continue
        author = str(n.get("author") or "").lower()
        if (
            author.endswith("[bot]")
            or author in ("github-actions", "codecov", "sonarcloud", "dependabot")
        ):
            continue
        body = (n.get("body") or "").strip()
        if not body:
            continue
        if "<!-- spec-driven-bot -->" in body or body.startswith("🤖 **Spec-Driven Dev"):
            continue
        comments.append(body)
    return comments
