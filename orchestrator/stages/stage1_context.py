"""
Stage 1 — Assemble Ticket Context.

Pulls ticket data from JIRA and the relevant source/test files from
GitLab.  Produces a ``TicketContext`` dataclass consumed by Stage 2.

Implements FR-3, FR-4.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.gitlab_client import GitLabClient
from orchestrator.jira_client import JiraClient, TicketData
from orchestrator.llm_client import LLMClient

logger = logging.getLogger(__name__)

# Glob patterns for JS/TS test files.
_TEST_PATTERNS = (
    "*.test.ts",
    "*.test.tsx",
    "*.spec.ts",
    "*.spec.tsx",
    "*.test.js",
    "*.test.jsx",
    "*.spec.js",
    "*.spec.jsx",
)

# Glob patterns for JS/TS source files (excluding tests, configs, assets).
_SOURCE_EXTENSIONS = (
    "*.ts",
    "*.tsx",
    "*.js",
    "*.jsx",
)

_IGNORE_DIRS = (
    "node_modules",
    "dist",
    "build",
    ".git",
    "coverage",
    "__pycache__",
)


@dataclass
class TicketContext:
    """Everything the downstream stages need to know about a ticket."""

    ticket: TicketData
    default_branch: str
    source_files: dict[str, str]  # {path: content}
    test_files: dict[str, str]    # {path: content}
    all_file_paths: list[str]     # full tree listing (paths only)


_INELIGIBLE_KEYWORDS = (
    ("database migration", "Database schema migration detected"),
    ("db migration", "Database schema migration detected"),
    ("alter table", "SQL/schema alteration detected"),
    ("schema overhaul", "Database schema changes detected"),
    ("jwt token authentication overhaul", "Security/Auth core overhaul detected"),
    ("cross-repo", "Multi-repository modification not supported"),
    ("multi-repo", "Multi-repository modification not supported"),
)


def _check_qualification(ticket: TicketData) -> str | None:
    """Return reason if ticket is ineligible for automation, or None if eligible."""
    content = f"{ticket.summary} {ticket.description}".lower()
    for kw, reason in _INELIGIBLE_KEYWORDS:
        if kw in content:
            return reason
    if not ticket.acceptance_criteria or not ticket.acceptance_criteria.strip():
        return "Missing acceptance criteria"
    return None


def run(
    ticket_id: str,
    jira: JiraClient,
    gitlab: GitLabClient,
    llm: LLMClient,
    checkpoint: Checkpoint,
    audit: AuditLogger,
    qualifying_label: str,
) -> TicketContext:
    """
    Execute Stage 1: fetch ticket data, enumerate repo, fetch relevant files.

    Parameters
    ----------
    ticket_id : str
        The JIRA ticket key (e.g. ``PROJ-123``).
    jira : JiraClient
        Initialised JIRA client.
    gitlab : GitLabClient
        Initialised GitLab client.
    llm : LLMClient
        Initialised LLM client (used for relevance ranking).
    checkpoint : Checkpoint
        Current checkpoint (updated on completion).
    audit : AuditLogger
        Audit log writer.
    qualifying_label : str
        The label the ticket must carry to be eligible.

    Returns
    -------
    TicketContext
    """
    audit.log(stage=1, event_type="stage1_start")

    # ── 1. Fetch ticket from JIRA ───────────────────────────────────────
    ticket = jira.get_ticket(ticket_id)
    logger.info("Ticket %s: %s", ticket.ticket_id, ticket.summary)

    if qualifying_label not in ticket.labels:
        raise ValueError(
            f"Ticket {ticket_id} does not carry the qualifying label "
            f"'{qualifying_label}'.  Labels present: {ticket.labels}"
        )

    # ── Qualification triage check (Phase 4) ───────────────────────────
    disqualification_reason = _check_qualification(ticket)
    if disqualification_reason:
        logger.warning(
            "Ticket %s failed qualification triage: %s",
            ticket_id,
            disqualification_reason,
        )
        jira.post_lifecycle_comment(
            ticket_id=ticket_id,
            title="Triage: Ineligible for Automation",
            message=(
                f"This ticket was flagged with `{qualifying_label}` but is "
                f"ineligible for spec-driven automation:\n"
                f"**Reason:** {disqualification_reason}\n\n"
                f"The label `{qualifying_label}` has been removed."
            ),
        )
        jira.remove_label(ticket_id, qualifying_label)
        audit.log(
            stage=1,
            event_type="stage1_triaged_ineligible",
            payload={"reason": disqualification_reason},
        )
        raise ValueError(
            f"Ticket {ticket_id} is ineligible for automation: {disqualification_reason}"
        )

    # ── 2. Enumerate repository file tree ───────────────────────────────
    default_branch = gitlab.get_default_branch()
    tree = gitlab.get_file_tree(ref=default_branch, recursive=True)
    all_paths = [f.path for f in tree if f.type == "blob"]

    # Filter out ignored directories.
    all_paths = [
        p for p in all_paths
        if not any(p.startswith(d + "/") or p.startswith(d + "\\") for d in _IGNORE_DIRS)
    ]

    # ── 3. Separate test files from source files ────────────────────────
    test_paths = [p for p in all_paths if _is_test_file(p)]
    source_paths = [p for p in all_paths if _is_source_file(p) and p not in test_paths]

    logger.info("Repo tree: %d total, %d source, %d test files",
                len(all_paths), len(source_paths), len(test_paths))

    # ── 4. Fetch test file contents (always — needed for context) ──────
    test_files: dict[str, str] = {}
    for path in test_paths:
        try:
            test_files[path] = gitlab.get_file_content(path, ref=default_branch)
        except Exception as exc:
            logger.warning("Could not fetch test file %s: %s", path, exc)

    # ── 5. Rank source files by relevance to the ticket ────────────────
    # For small repos, just fetch everything.  For large repos, use the
    # LLM to pick the most relevant files.
    if len(source_paths) <= 30:
        relevant_paths = source_paths
    else:
        relevant_paths = _rank_relevant_files(
            llm, ticket, source_paths, max_files=30,
        )

    source_files: dict[str, str] = {}
    for path in relevant_paths:
        try:
            source_files[path] = gitlab.get_file_content(path, ref=default_branch)
        except Exception as exc:
            logger.warning("Could not fetch source file %s: %s", path, exc)

    # ── 6. Build context and update checkpoint ─────────────────────────
    context = TicketContext(
        ticket=ticket,
        default_branch=default_branch,
        source_files=source_files,
        test_files=test_files,
        all_file_paths=all_paths,
    )

    checkpoint.current_stage = 2
    audit.log(
        stage=1,
        event_type="stage1_complete",
        payload={
            "source_file_count": len(source_files),
            "test_file_count": len(test_files),
            "total_files": len(all_paths),
        },
    )
    logger.info("Stage 1 complete for %s", ticket_id)
    return context


# ── Helpers ─────────────────────────────────────────────────────────────


def _is_test_file(path: str) -> bool:
    """Check whether *path* matches any JS/TS test-file glob."""
    basename = path.rsplit("/", 1)[-1] if "/" in path else path
    return any(fnmatch.fnmatch(basename, pat) for pat in _TEST_PATTERNS)


def _is_source_file(path: str) -> bool:
    """Check whether *path* is a JS/TS source file (not a config/asset)."""
    basename = path.rsplit("/", 1)[-1] if "/" in path else path
    return any(fnmatch.fnmatch(basename, pat) for pat in _SOURCE_EXTENSIONS)


def _rank_relevant_files(
    llm: LLMClient,
    ticket: TicketData,
    paths: list[str],
    max_files: int = 30,
) -> list[str]:
    """
    Use the LLM to pick the most relevant source files for the ticket.

    Sends a lightweight prompt with the file list and ticket summary;
    asks the model to return the top-N most likely relevant paths.
    """
    paths_text = "\n".join(paths)
    prompt = (
        "You are a code analysis assistant.  Given the following JIRA ticket "
        "and a list of file paths in a JavaScript/TypeScript/React repository, "
        "return the paths most likely to need changes to resolve this ticket.\n\n"
        f"## Ticket\n"
        f"**Summary:** {ticket.summary}\n"
        f"**Description:** {ticket.description}\n"
        f"**Acceptance Criteria:** {ticket.acceptance_criteria}\n\n"
        f"## File Paths\n```\n{paths_text}\n```\n\n"
        f"Return up to {max_files} file paths, one per line, no numbering, "
        f"no explanation.  Only return paths from the list above."
    )

    response = llm.chat(prompt)
    content = response.get("content", "")

    selected: list[str] = []
    valid_set = set(paths)
    for line in content.strip().splitlines():
        line = line.strip().strip("`").strip()
        if line in valid_set:
            selected.append(line)
        if len(selected) >= max_files:
            break

    # If the LLM returned too few, pad with the original list.
    if len(selected) < max_files:
        for p in paths:
            if p not in selected:
                selected.append(p)
            if len(selected) >= max_files:
                break

    logger.info("LLM-ranked %d relevant source files (from %d total)", len(selected), len(paths))
    return selected
