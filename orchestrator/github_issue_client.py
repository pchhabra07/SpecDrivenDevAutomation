"""
GitHub Issues API client — wraps ``PyGithub`` for issue-driven automation.

Replaces JiraClient with native GitHub Issues:
- Fetches issues by number or ID
- Extracts Acceptance Criteria from issue body markdown headers or checklists
- Manages issue labels (including removing qualification labels on triage rejection)
- Posts formatted lifecycle comments linking to GitHub Pull Requests
- Transitions issue states (open/closed) and status labels
"""

from __future__ import annotations

import logging
import re
from typing import Any

from github import Auth, Github, GithubException

from orchestrator.jira_client import TicketData

logger = logging.getLogger(__name__)


class GitHubIssueClient:
    """High-level GitHub Issues facade for ticket lifecycle operations."""

    @staticmethod
    def _clean_repo(repo: str) -> str:
        clean = str(repo).strip()
        if "github.com" in clean:
            clean = clean.split("github.com", 1)[1].lstrip("/:").removesuffix(".git")
        return clean

    def __init__(
        self,
        token: str,
        repo: str,
        base_url: str = "https://api.github.com",
    ) -> None:
        if token:
            auth = Auth.Token(token)
            self._gh = Github(auth=auth, base_url=base_url)
        else:
            self._gh = Github(base_url=base_url)

        self._repo_name = self._clean_repo(repo)
        self._repo = self._gh.get_repo(self._repo_name)
        logger.info("GitHub Issue client initialised for %s", self._repo.full_name)

    # ── Issue Number Parsing ────────────────────────────────────────────

    @staticmethod
    def parse_issue_number(ticket_id: str | int) -> int:
        """
        Extract numeric issue number from string like '123', '#123', or 'PROJ-123'.
        """
        if isinstance(ticket_id, int):
            return ticket_id
        clean = str(ticket_id).strip().lstrip("#")
        if clean.isdigit():
            return int(clean)
        # Handle 'PREFIX-123'
        match = re.search(r"(\d+)$", clean)
        if match:
            return int(match.group(1))
        raise ValueError(f"Cannot extract GitHub issue number from '{ticket_id}'")

    # ── Ticket retrieval ────────────────────────────────────────────────

    def get_ticket(self, ticket_id: str | int) -> TicketData:
        """
        Fetch a GitHub Issue and extract pipeline-relevant fields into TicketData.
        """
        issue_num = self.parse_issue_number(ticket_id)
        issue = self._repo.get_issue(issue_num)

        summary = issue.title or ""
        description = issue.body or ""
        labels = [l.name for l in issue.labels]

        # Extract components from labels (e.g. 'component:Payments' or 'Payments')
        components: list[str] = []
        for l in labels:
            lower = l.lower()
            if lower.startswith("component:"):
                components.append(l.split(":", 1)[1].strip())

        assignee = issue.assignee.login if issue.assignee else None
        reporter = issue.user.login if issue.user else None
        status = issue.state or "open"

        acceptance = self._extract_acceptance_criteria(description)

        ticket = TicketData(
            ticket_id=str(ticket_id),
            summary=summary,
            description=description,
            acceptance_criteria=acceptance,
            labels=labels,
            components=components,
            assignee=assignee,
            reporter=reporter,
            status=status,
            raw={"number": issue.number, "html_url": issue.html_url, "state": issue.state},
        )
        logger.info("Fetched GitHub issue #%d: %s", issue_num, summary)
        return ticket

    def post_comment(self, ticket_id: str | int, body: str) -> None:
        """Post a comment to the GitHub issue."""
        issue_num = self.parse_issue_number(ticket_id)
        issue = self._repo.get_issue(issue_num)
        issue.create_comment(body)
        logger.info("Comment posted on GitHub issue #%d", issue_num)

    def transition_ticket(
        self,
        ticket_id: str | int,
        transition_name: str | list[str],
    ) -> bool:
        """
        Update issue state or apply status labels.
        """
        issue_num = self.parse_issue_number(ticket_id)
        issue = self._repo.get_issue(issue_num)

        candidates = (
            [transition_name] if isinstance(transition_name, str) else transition_name
        )
        norm_candidates = [c.lower() for c in candidates]

        # Check for closing transitions
        if any("close" in c or "done" in c or "resolved" in c for c in norm_candidates):
            issue.edit(state="closed")
            logger.info("GitHub issue #%d marked as closed", issue_num)
            return True

        # Check for in-progress or review transitions
        if any("progress" in c for c in norm_candidates):
            issue.add_to_labels("in-progress")
            logger.info("GitHub issue #%d marked 'in-progress'", issue_num)
            return True

        if any("review" in c for c in norm_candidates):
            issue.add_to_labels("under-review")
            logger.info("GitHub issue #%d marked 'under-review'", issue_num)
            return True

        return True

    def remove_label(self, ticket_id: str | int, label: str) -> None:
        """Remove a label from the GitHub issue (e.g. during triage rejection)."""
        issue_num = self.parse_issue_number(ticket_id)
        issue = self._repo.get_issue(issue_num)
        try:
            issue.remove_from_labels(label)
            logger.info("Removed label '%s' from issue #%d", label, issue_num)
        except GithubException as exc:
            logger.warning("Could not remove label '%s' from issue #%d: %s", label, issue_num, exc)

    def post_lifecycle_comment(
        self,
        ticket_id: str | int,
        title: str,
        message: str,
        mr_url: str | None = None,
    ) -> None:
        """Post a standardized, formatted lifecycle comment on the GitHub issue."""
        body = f"### 🤖 Spec-Driven Automation — {title}\n\n{message}"
        if mr_url:
            body += f"\n\n**GitHub Pull Request:** [View Pull Request]({mr_url})"
        self.post_comment(ticket_id, body)

    def has_qualifying_label(self, ticket_id: str | int, label: str) -> bool:
        """Check whether the issue carries the qualifying automation label."""
        ticket = self.get_ticket(ticket_id)
        return label in ticket.labels

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_acceptance_criteria(body: str) -> str:
        """
        Extract Acceptance Criteria from markdown issue body.
        Looks for '## Acceptance Criteria' or checklist items.
        """
        if not body:
            return ""

        # Pattern 1: Section under ## or ### Acceptance Criteria
        match = re.search(
            r"#{1,3}\s+Acceptance\s+Criteria\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
            body,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            criteria = match.group(1).strip()
            if criteria:
                return criteria

        # Pattern 2: Markdown task checklists (- [ ] ...)
        checklist_items = re.findall(r"^[*-]\s*\[[ xX]\].*$", body, re.MULTILINE)
        if checklist_items:
            return "\n".join(checklist_items)

        # Fallback to full body
        return body.strip()
