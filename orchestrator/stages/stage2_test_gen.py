"""
Stage 2 — Generate Test Cases (Pre-Development).

Prompts the LLM to generate Jest test cases driven by the ticket's
specification and acceptance criteria — *before* any code changes are
made.  The proposed tests are committed to a dedicated branch and
presented as a GitLab MR for human review (Gate 1).

Once the reviewer approves the MR, the test files are "locked" by
recording their SHA-256 hashes in the checkpoint.  From this point
forward the coding stage (Stage 3) is forbidden from modifying these
files.

If the reviewer requests changes, the LLM is re-prompted with the
reviewer's comments, generates updated tests, force-pushes to the
same branch, and waits for approval again.

Implements FR-5, FR-6, FR-7, FR-8.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.gitlab_client import GitLabClient
from orchestrator.gitlab_gate import GateOutcome, wait_for_approval
from orchestrator.llm_client import LLMClient
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.tamper_check import build_lock_hashes

logger = logging.getLogger(__name__)

# Maximum Gate-1 review-revision cycles before aborting.
_MAX_REVISION_CYCLES = 5


def run(
    context: TicketContext,
    gitlab: GitLabClient,
    llm: LLMClient,
    checkpoint: Checkpoint,
    audit: AuditLogger,
    gate_poll_interval: int = 30,
    gate_timeout_hours: float = 48.0,
) -> dict[str, str]:
    """
    Execute Stage 2: generate tests → open MR → await approval → lock.

    Returns
    -------
    dict[str, str]
        ``{file_path: content}`` of the approved & locked test files.
    """
    audit.log(stage=2, event_type="stage2_start")
    ticket = context.ticket

    # ── 1. Generate test cases via LLM ──────────────────────────────────
    generated_tests = _generate_tests(llm, context)
    logger.info("LLM generated %d test file(s)", len(generated_tests))

    # ── 2. Create the tests-only branch ─────────────────────────────────
    tests_branch = f"automation/{ticket.ticket_id}/tests"
    if not gitlab.branch_exists(tests_branch):
        gitlab.create_branch(tests_branch, ref=context.default_branch)
    checkpoint.tests_branch = tests_branch

    # ── 3. Commit test files ────────────────────────────────────────────
    gitlab.commit_files(
        branch=tests_branch,
        files=generated_tests,
        commit_message=f"[spec-driven] Generate tests for {ticket.ticket_id}",
    )

    # ── 4. Open a draft MR for Gate 1 ──────────────────────────────────
    if checkpoint.gate1_mr_iid is None:
        mr_info = gitlab.create_mr(
            source_branch=tests_branch,
            target_branch=context.default_branch,
            title=f"[Gate 1] Tests for {ticket.ticket_id}: {ticket.summary}",
            description=_build_mr_description(ticket, generated_tests),
            draft=True,
        )
        checkpoint.gate1_mr_iid = mr_info.iid
        audit.log(stage=2, event_type="gate1_opened", payload={"mr_iid": mr_info.iid})
    else:
        # MR already exists (e.g. resumed from checkpoint).
        logger.info("Reusing existing Gate 1 MR !%d", checkpoint.gate1_mr_iid)

    # ── 5. Wait for human approval (with revision loop) ────────────────
    approved_tests = generated_tests
    for cycle in range(1, _MAX_REVISION_CYCLES + 1):
        logger.info("Gate 1 approval cycle %d/%d", cycle, _MAX_REVISION_CYCLES)

        gate_result = wait_for_approval(
            gitlab=gitlab,
            mr_iid=checkpoint.gate1_mr_iid,
            poll_interval_seconds=gate_poll_interval,
            timeout_hours=gate_timeout_hours,
        )

        if gate_result.outcome == GateOutcome.APPROVED:
            logger.info("Gate 1 APPROVED for %s", ticket.ticket_id)
            audit.log(
                stage=2,
                event_type="gate1_approved",
                payload={"approved_by": gate_result.approved_by, "cycle": cycle},
            )
            break

        # Reviewer left comments but did not approve — treat as change request.
        if gate_result.reviewer_comments:
            logger.info(
                "Reviewer requested changes (%d comments). Re-generating.",
                len(gate_result.reviewer_comments),
            )
            audit.log(
                stage=2,
                event_type="gate1_changes_requested",
                payload={"comments": gate_result.reviewer_comments, "cycle": cycle},
            )
            approved_tests = _revise_tests(
                llm, context, approved_tests, gate_result.reviewer_comments,
            )
            gitlab.commit_files(
                branch=tests_branch,
                files=approved_tests,
                commit_message=(
                    f"[spec-driven] Revise tests for {ticket.ticket_id} "
                    f"(revision {cycle})"
                ),
            )
    else:
        raise RuntimeError(
            f"Gate 1 for {ticket.ticket_id} not approved after "
            f"{_MAX_REVISION_CYCLES} revision cycles."
        )

    # ── 6. Lock test files ──────────────────────────────────────────────
    locked_hashes = build_lock_hashes(approved_tests)
    checkpoint.locked_test_hashes = locked_hashes
    checkpoint.gate1_approved = True
    checkpoint.current_stage = 3

    audit.log(
        stage=2,
        event_type="tests_locked",
        payload={"locked_files": list(locked_hashes.keys())},
    )
    logger.info("Stage 2 complete — %d test file(s) locked.", len(locked_hashes))
    return approved_tests


# ── LLM prompt helpers ──────────────────────────────────────────────────


def _generate_tests(llm: LLMClient, context: TicketContext) -> dict[str, str]:
    """Prompt the LLM to generate Jest test cases from the ticket spec."""
    ticket = context.ticket

    # Build a summary of existing tests for context.
    existing_tests_summary = ""
    if context.test_files:
        existing_tests_summary = (
            "## Existing Test Files\n"
            + "\n".join(
                f"### `{path}`\n```typescript\n{content}\n```"
                for path, content in list(context.test_files.items())[:10]
            )
        )

    # Build a summary of relevant source files.
    source_summary = ""
    if context.source_files:
        source_summary = (
            "## Relevant Source Files\n"
            + "\n".join(
                f"### `{path}`\n```typescript\n{content}\n```"
                for path, content in list(context.source_files.items())[:15]
            )
        )

    prompt = (
        "You are a senior test engineer specialising in JavaScript/TypeScript, "
        "React, and Redux.  Your task is to generate comprehensive Jest test "
        "cases based **solely** on the ticket's specification and acceptance "
        "criteria below — NOT from the existing code.\n\n"
        "## JIRA Ticket\n"
        f"**ID:** {ticket.ticket_id}\n"
        f"**Summary:** {ticket.summary}\n"
        f"**Description:**\n{ticket.description}\n\n"
        f"**Acceptance Criteria:**\n{ticket.acceptance_criteria}\n\n"
        f"{source_summary}\n\n"
        f"{existing_tests_summary}\n\n"
        "## Instructions\n"
        "1. Generate unit tests, and integration/regression tests where applicable.\n"
        "2. Each test must directly trace back to an acceptance criterion.\n"
        "3. Use Jest + React Testing Library where React components are involved.\n"
        "4. Follow the naming convention: `<module>.test.ts` or `<module>.test.tsx`.\n"
        "5. Do NOT modify existing test files — only create new test files or "
        "   clearly mark additions to existing ones.\n"
        "6. Include clear `describe` / `it` blocks with descriptive names.\n\n"
        "## Output Format\n"
        "Return your answer as one or more fenced code blocks, each preceded by "
        "a single-line file path comment like:\n"
        "```\n"
        "// FILE: src/__tests__/myModule.test.ts\n"
        "```\n"
        "followed by the complete file content.\n"
        "Do not include any explanation outside of code blocks."
    )

    response = llm.chat(prompt)
    content = response.get("content", "")
    return _parse_test_files(content)


def _revise_tests(
    llm: LLMClient,
    context: TicketContext,
    current_tests: dict[str, str],
    reviewer_comments: list[str],
) -> dict[str, str]:
    """Re-prompt the LLM with reviewer feedback to revise tests."""
    comments_text = "\n".join(f"- {c}" for c in reviewer_comments)
    current_tests_text = "\n".join(
        f"### `{path}`\n```typescript\n{content}\n```"
        for path, content in current_tests.items()
    )

    prompt = (
        "A human reviewer has requested changes to the test cases you generated "
        "earlier.  Here are their comments:\n\n"
        f"{comments_text}\n\n"
        "Here are the current test files:\n\n"
        f"{current_tests_text}\n\n"
        "Please revise the tests to address the reviewer's feedback. "
        "Return the complete updated files in the same format as before "
        "(fenced code blocks with `// FILE:` path comments)."
    )

    response = llm.chat(prompt)
    content = response.get("content", "")
    revised = _parse_test_files(content)

    # Merge: updated files override, untouched files carry over.
    merged = dict(current_tests)
    merged.update(revised)
    return merged


def _parse_test_files(llm_output: str) -> dict[str, str]:
    """
    Parse the LLM's response into ``{file_path: content}`` pairs.

    Expected format — each file is preceded by a ``// FILE: <path>`` comment
    and wrapped in a fenced code block:

        // FILE: src/__tests__/foo.test.ts
        ```typescript
        import ...
        ```
    """
    files: dict[str, str] = {}
    lines = llm_output.split("\n")
    current_path: str | None = None
    current_content: list[str] = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        # Detect file-path marker.
        if stripped.startswith("// FILE:"):
            # Save previous file if any.
            if current_path and current_content:
                files[current_path] = "\n".join(current_content)
            current_path = stripped.removeprefix("// FILE:").strip()
            current_content = []
            in_code_block = False
            continue

        # Detect code fence boundaries.
        if stripped.startswith("```"):
            if in_code_block:
                # Closing fence — end of current file content.
                in_code_block = False
                if current_path and current_content:
                    files[current_path] = "\n".join(current_content)
                    current_content = []
                continue
            else:
                in_code_block = True
                continue

        if in_code_block and current_path is not None:
            current_content.append(line)

    # Handle last file if the LLM forgot the closing fence.
    if current_path and current_content:
        files[current_path] = "\n".join(current_content)

    return files


def _build_mr_description(ticket: Any, test_files: dict[str, str]) -> str:
    """Build the Gate-1 MR description body."""
    file_list = "\n".join(f"- `{path}`" for path in test_files)
    return (
        f"## Spec-Driven Test Generation — Gate 1 Review\n\n"
        f"**JIRA Ticket:** {ticket.ticket_id} — {ticket.summary}\n\n"
        f"### Acceptance Criteria\n{ticket.acceptance_criteria}\n\n"
        f"### Generated Test Files\n{file_list}\n\n"
        f"---\n"
        f"Please review these tests for correctness and completeness "
        f"against the acceptance criteria.  **Approve this MR** to lock "
        f"the tests and proceed to automated code generation.\n\n"
        f"Any comments will be routed back to the AI agent for revision."
    )
