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

    def transition_ticket(self, ticket_id: str, transition_name: str) -> None:
        """
        Transition the ticket to a new status.

        Looks up the transition ID by name, then executes it.
        """
        transitions = self._jira.get_issue_transitions(ticket_id)
        target = None
        for t in transitions:
            if t["name"].lower() == transition_name.lower():
                target = t["id"]
                break
        if target is None:
            logger.warning(
                "Transition '%s' not found for %s. Available: %s",
                transition_name,
                ticket_id,
                [t["name"] for t in transitions],
            )
            return
        self._jira.set_issue_status(ticket_id, target)
        logger.info("Ticket %s transitioned to '%s'", ticket_id, transition_name)

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
