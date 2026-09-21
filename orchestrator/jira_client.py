"""
JIRA REST API client.

Provides ticket retrieval, comment posting, and status transitions.
Uses JIRA REST API v3 via ``atlassian-python-api``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from atlassian import Jira

logger = logging.getLogger(__name__)


@dataclass
class TicketData:
    """Structured representation of a JIRA ticket relevant to the pipeline."""

    ticket_id: str
    summary: str
    description: str
    acceptance_criteria: str
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    assignee: str | None = None
    reporter: str | None = None
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class JiraClient:
    """High-level JIRA facade scoped to pipeline needs."""

    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self._jira = Jira(url=base_url, username=email, password=api_token)
        logger.info("JIRA client initialised for %s", base_url)

    def get_ticket(self, ticket_id: str) -> TicketData:
        """
        Fetch a JIRA ticket and extract pipeline-relevant fields.

        Acceptance criteria are looked up in the following order:
        1. A custom field named ``Acceptance Criteria`` (common in Agile boards).
        2. A section headed ``## Acceptance Criteria`` or ``### Acceptance Criteria``
           within the description body.
        3. Falls back to the full description if neither is found.
        """
        issue = self._jira.issue(ticket_id)
        fields = issue.get("fields", {})

        summary = fields.get("summary", "")
        description = fields.get("description", "") or ""
        labels = fields.get("labels", [])
        components = [
            c["name"]
            for c in fields.get("components", [])
            if isinstance(c, dict) and "name" in c
        ]
        assignee = (fields.get("assignee") or {}).get("displayName")
        reporter = (fields.get("reporter") or {}).get("displayName")
        status = (fields.get("status") or {}).get("name", "")

        # Try to extract acceptance criteria.
        acceptance = self._extract_acceptance_criteria(fields, description)

        ticket = TicketData(
            ticket_id=ticket_id,
            summary=summary,
            description=description,
            acceptance_criteria=acceptance,
            labels=labels,
            components=components,
            assignee=assignee,
            reporter=reporter,
            status=status,
            raw=issue,
        )
        logger.info("Fetched ticket %s: %s", ticket_id, summary)
        return ticket

    def post_comment(self, ticket_id: str, body: str) -> None:
        """Post a comment on the ticket (used for escalation summaries)."""
        self._jira.issue_add_comment(ticket_id, body)
        logger.info("Comment posted on %s", ticket_id)

    def find_transition(
        self,
        ticket_id: str,
        candidate_names: list[str],
    ) -> tuple[str, str] | None:
        """
        Find a valid transition ID matching any name in candidate_names.

        Returns (transition_id, matched_name) or None if none match.
        """
        try:
            transitions = self._jira.get_issue_transitions(ticket_id)
        except Exception as exc:
            logger.warning("Failed to fetch transitions for %s: %s", ticket_id, exc)
            return None

        # Try exact case-insensitive match in priority order
        for candidate in candidate_names:
            c_norm = candidate.strip().lower()
            for t in transitions:
                if t.get("name", "").strip().lower() == c_norm:
                    return t["id"], t["name"]

        # Try substring match
        for candidate in candidate_names:
            c_norm = candidate.strip().lower()
            for t in transitions:
                if c_norm in t.get("name", "").strip().lower():
                    return t["id"], t["name"]

        return None

    def transition_ticket(
        self,
        ticket_id: str,
        transition_name: str | list[str],
    ) -> bool:
        """
        Transition the ticket to a new status.

        Accepts either a single transition name or a list of fallback candidates.
        Returns True if transitioned successfully, False otherwise.
        """
        candidates = [transition_name] if isinstance(transition_name, str) else transition_name
        match = self.find_transition(ticket_id, candidates)
        if match is None:
            logger.warning(
                "None of transitions %s found for %s.",
                candidates,
                ticket_id,
            )
            return False

        transition_id, matched_name = match
        try:
            self._jira.set_issue_status(ticket_id, transition_id)
            logger.info("Ticket %s transitioned via '%s'", ticket_id, matched_name)
            return True
        except Exception as exc:
            logger.error("Failed to execute transition '%s' for %s: %s", matched_name, ticket_id, exc)
            return False

    def remove_label(self, ticket_id: str, label: str) -> None:
        """Remove a label from the ticket (used during triage rejection)."""
        try:
            issue = self._jira.issue(ticket_id)
            current_labels = (issue.get("fields") or {}).get("labels", [])
            if label in current_labels:
                new_labels = [l for l in current_labels if l != label]
                self._jira.update_issue_field(ticket_id, {"labels": new_labels})
                logger.info("Removed label '%s' from %s", label, ticket_id)
        except Exception as exc:
            logger.warning("Could not remove label '%s' from %s: %s", label, ticket_id, exc)

    def post_lifecycle_comment(
        self,
        ticket_id: str,
        title: str,
        message: str,
        mr_url: str | None = None,
    ) -> None:
        """Post a standardized, formatted lifecycle comment on the ticket."""
        body = f"*🤖 Spec-Driven Automation — {title}*\n\n{message}"
        if mr_url:
            body += f"\n\n*GitLab Merge Request:* [View MR|{mr_url}]"
        self.post_comment(ticket_id, body)

    def has_qualifying_label(self, ticket_id: str, label: str) -> bool:
        """Check whether the ticket carries the qualifying automation label."""
        ticket = self.get_ticket(ticket_id)
        return label in ticket.labels

    # ── Internals ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_acceptance_criteria(
        fields: dict[str, Any],
        description: str,
    ) -> str:
        """
        Best-effort extraction of acceptance criteria from JIRA fields.
        """
        # 1. Check for a custom field (varies per JIRA instance).
        for key, value in fields.items():
            if key.startswith("customfield_") and isinstance(value, str):
                # Heuristic: if the field name hint or value looks like AC.
                if "acceptance" in key.lower() or "criteria" in key.lower():
                    return value

        # 2. Parse a markdown-style section from the description.
        lower = description.lower()
        for marker in ("## acceptance criteria", "### acceptance criteria", "acceptance criteria:"):
            idx = lower.find(marker)
            if idx != -1:
                # Return everything from the marker to the next heading or end.
                after = description[idx + len(marker):]
                # Trim at the next heading, if any.
                for heading in ("\n## ", "\n### ", "\n# "):
                    end = after.find(heading)
                    if end != -1:
                        after = after[:end]
                return after.strip()

        # 3. Fallback to the full description.
        return description
