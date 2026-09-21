"""
Dynamic Repository Resolver (Phase 5).

Enables multi-repository scaling across team boundaries.
Resolves GitLab project IDs dynamically from JIRA ticket components, labels,
or project keys using the mapping configured in `REPO_MAPPING`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from orchestrator.jira_client import TicketData

logger = logging.getLogger(__name__)


class RepoResolver:
    """Resolves target GitLab repo/project ID for a given JIRA ticket."""

    def __init__(self, mapping_json: str | dict[str, Any] = "{}") -> None:
        if isinstance(mapping_json, dict):
            self._mapping: dict[str, int] = {
                str(k).strip().lower(): int(v)
                for k, v in mapping_json.items()
                if str(v).isdigit()
            }
        elif isinstance(mapping_json, str):
            try:
                raw = json.loads(mapping_json or "{}")
                if isinstance(raw, dict):
                    self._mapping = {
                        str(k).strip().lower(): int(v)
                        for k, v in raw.items()
                        if str(v).isdigit()
                    }
                else:
                    logger.warning("REPO_MAPPING is not a JSON object: %s", mapping_json)
                    self._mapping = {}
            except Exception as exc:
                logger.warning(
                    "Failed to parse REPO_MAPPING JSON: %s (err: %s)",
                    mapping_json,
                    exc,
                )
                self._mapping = {}
        else:
            self._mapping = {}

    @property
    def mapping(self) -> dict[str, int]:
        """Return read-only copy of parsed repository mapping."""
        return dict(self._mapping)

    def resolve(self, ticket: TicketData, default_repo_id: int) -> int:
        """
        Determine the GitLab project ID for a ticket.

        Matching priority:
        1. Exact match on JIRA component name (case-insensitive).
        2. Exact match on JIRA labels (case-insensitive, e.g. "auth-service" or "repo:12345").
        3. Match on JIRA ticket project key (e.g. "AUTH" from "AUTH-101").
        4. Fall back to default_repo_id.
        """
        if not self._mapping:
            return default_repo_id

        # 1. Match by component
        for comp in getattr(ticket, "components", []):
            if comp:
                key = comp.strip().lower()
                if key in self._mapping:
                    matched_id = self._mapping[key]
                    logger.info(
                        "Resolved ticket %s to repo ID %d via component '%s'",
                        ticket.ticket_id,
                        matched_id,
                        comp,
                    )
                    return matched_id

        # 2. Match by label
        for label in getattr(ticket, "labels", []):
            if label:
                norm_label = label.strip().lower()
                # Direct match
                if norm_label in self._mapping:
                    matched_id = self._mapping[norm_label]
                    logger.info(
                        "Resolved ticket %s to repo ID %d via label '%s'",
                        ticket.ticket_id,
                        matched_id,
                        label,
                    )
                    return matched_id
                # Prefix match: e.g. "repo:auth-service" or "repo:98765"
                if norm_label.startswith("repo:"):
                    target = norm_label[5:].strip()
                    if target.isdigit():
                        return int(target)
                    if target in self._mapping:
                        return self._mapping[target]

        # 3. Match by ticket project key (e.g. "PROJ" from "PROJ-123")
        if "-" in ticket.ticket_id:
            proj_key = ticket.ticket_id.split("-", 1)[0].strip().lower()
            if proj_key in self._mapping:
                matched_id = self._mapping[proj_key]
                logger.info(
                    "Resolved ticket %s to repo ID %d via project key '%s'",
                    ticket.ticket_id,
                    matched_id,
                    proj_key,
                )
                return matched_id

        logger.debug(
            "No custom repo match found for ticket %s; using default repo ID %d",
            ticket.ticket_id,
            default_repo_id,
        )
        return default_repo_id
