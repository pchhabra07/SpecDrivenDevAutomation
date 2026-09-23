"""
Stage 2 — Generate Test Cases (Pre-Development).

Prompts the LLM to generate Jest test cases driven by the ticket's
specification and acceptance criteria — *before* any code changes are
made.  The proposed tests are committed to a dedicated branch and
presented as a PR/MR for human review (Gate 1).

Once the reviewer approves, the test files are "locked" by recording
their SHA-256 hashes in the checkpoint.  From this point forward the
coding stage (Stage 3) is forbidden from modifying these files.

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
from orchestrator.gitlab_gate import GateOutcome, wait_for_approval
from orchestrator.llm_client import LLMClient
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.tamper_check import build_lock_hashes

logger = logging.getLogger(__name__)

# Maximum Gate-1 review-revision cycles before aborting.
_MAX_REVISION_CYCLES = 5


def run(
    context: TicketContext,
    gitlab: Any,
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
    logger.info("Calling LLM to generate test cases…")
    generated_tests = _generate_tests(llm, context)
    logger.info("LLM generated %d test file(s)", len(generated_tests))

    if not generated_tests:
        logger.error("LLM returned no test files for ticket %s", ticket.ticket_id)
        raise RuntimeError(
            f"Stage 2 failed: LLM did not generate any test files matching the expected format for ticket {ticket.ticket_id}."
        )

    # ── 2. Create the tests-only branch ─────────────────────────────────
    logger.info("Creating test branch…")
    tests_branch = f"automation/{ticket.ticket_id}/tests"
    if not gitlab.branch_exists(tests_branch):
        gitlab.create_branch(tests_branch, ref=context.default_branch)
    checkpoint.tests_branch = tests_branch

    # Ensure CI workflow exists for target repository
    from orchestrator.ci_generator import ensure_target_ci_workflow

    repo_id = getattr(gitlab, "project_id", None) or getattr(gitlab, "repo_name", "target-repo")
    ci_workflow_path, ci_created = ensure_target_ci_workflow(
        gitlab=gitlab,
        branch=tests_branch,
        repo_identifier=repo_id,
        all_file_paths=context.all_file_paths,
    )

    # ── 3. Commit test files ────────────────────────────────────────────
    logger.info("Committing generated tests to branch %s", tests_branch)
    gitlab.commit_files(
        branch=tests_branch,
        files=generated_tests,
        commit_message=f"[spec-driven] Generate tests for {ticket.ticket_id}",
    )

    # ── 4. Open a draft PR/MR for Gate 1 ──────────────────────────────
    if checkpoint.gate1_mr_iid is None:
        logger.info("Opening draft PR for Gate 1 review…")
        mr_info = gitlab.create_mr(
            source_branch=tests_branch,
            target_branch=context.default_branch,
            title=f"[Gate 1] Tests for {ticket.ticket_id}: {ticket.summary}",
            description=_build_mr_description(
                ticket,
                generated_tests,
                ci_workflow_path=ci_workflow_path if ci_created else None,
            ),
            draft=True,
        )
        checkpoint.gate1_mr_iid = mr_info.iid
        audit.log(stage=2, event_type="gate1_opened", payload={"mr_iid": mr_info.iid})
    else:
        # PR/MR already exists (e.g. resumed from checkpoint).
        logger.info("Reusing existing Gate 1 PR #%d", checkpoint.gate1_mr_iid)

    # ── 5. Wait for human approval (with revision loop) ────────────────
    logger.info("Waiting for Gate 1 human approval…")
    approved_tests = generated_tests
    seen_comments: set[str] = set()
    for cycle in range(1, _MAX_REVISION_CYCLES + 1):
        logger.info("Gate 1 approval cycle %d/%d", cycle, _MAX_REVISION_CYCLES)

        gate_result = wait_for_approval(
            gitlab=gitlab,
            mr_iid=checkpoint.gate1_mr_iid,
            poll_interval_seconds=gate_poll_interval,
            timeout_hours=gate_timeout_hours,
            return_on_changes_requested=True,
            seen_comments=seen_comments,
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
            revised_tests = _revise_tests(
                llm, context, approved_tests, gate_result.reviewer_comments,
            )

            # Remove obsolete files from git branch if they were dropped during revision
            removed_files = set(approved_tests.keys()) - set(revised_tests.keys())
            for rm_path in removed_files:
                if hasattr(gitlab, "delete_file"):
                    gitlab.delete_file(
                        path=rm_path,
                        branch=tests_branch,
                        message=f"[spec-driven] Remove obsolete test {rm_path} per reviewer feedback",
                    )

            approved_tests = revised_tests

            gitlab.commit_files(
                branch=tests_branch,
                files=approved_tests,
                commit_message=(
                    f"[spec-driven] Revise tests for {ticket.ticket_id} "
                    f"(revision {cycle})"
                ),
            )

            # Post a comment back on the PR to notify the human reviewer of the update
            file_bullets = "\n".join(f"- `{f}`" for f in approved_tests)
            removed_bullets = (
                f"\n\n**Removed Files:**\n" + "\n".join(f"- `{f}`" for f in removed_files)
                if removed_files
                else ""
            )
            bot_msg = (
                f"<!-- spec-driven-bot -->\n"
                f"🤖 **Spec-Driven Dev Orchestrator** (Revision {cycle}/{_MAX_REVISION_CYCLES})\n\n"
                f"I have updated the test suite based on your feedback.\n\n"
                f"**Active Test Files:**\n{file_bullets}{removed_bullets}\n\n"
                f"Please review the updated tests. If everything looks good, approve this PR to proceed."
            )
            if hasattr(gitlab, "add_mr_comment"):
                gitlab.add_mr_comment(checkpoint.gate1_mr_iid, bot_msg)
            elif hasattr(gitlab, "add_pr_comment"):
                gitlab.add_pr_comment(checkpoint.gate1_mr_iid, bot_msg)
            seen_comments.add(bot_msg)
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


# ── Project stack detection helper ─────────────────────────────────────


def _detect_project_stack(context: TicketContext) -> str:
    """Identify the primary technology stack of the target repository."""
    paths = [p.lower() for p in context.all_file_paths]

    has_pkg_json = any(p == "package.json" or p.endswith("/package.json") for p in paths)
    if has_pkg_json:
        has_react = any(
            p.endswith((".tsx", ".jsx")) or "react" in p
            for p in paths
        )
        return "react" if has_react else "node"

    has_html_css = any(p.endswith((".html", ".htm", ".css")) for p in paths)
    if has_html_css and not has_pkg_json:
        return "static_web"

    has_python = any(
        p.endswith(".py") or p in ("pyproject.toml", "requirements.txt", "setup.py")
        for p in paths
    )
    if has_python:
        return "python"

    return "generic"


# ── LLM prompt helpers ──────────────────────────────────────────────────


def _generate_tests(llm: LLMClient, context: TicketContext) -> dict[str, str]:
    """Prompt the LLM to generate test cases from the ticket spec."""
    ticket = context.ticket
    stack = _detect_project_stack(context)

    # Build a summary of existing tests for context.
    existing_tests_summary = ""
    if context.test_files:
        existing_tests_summary = (
            "## Existing Test Files\n"
            + "\n".join(
                f"### `{path}`\n```\n{content}\n```"
                for path, content in list(context.test_files.items())[:10]
            )
        )

    # Build a summary of relevant source files.
    source_summary = ""
    if context.source_files:
        source_summary = (
            "## Relevant Source Files\n"
            + "\n".join(
                f"### `{path}`\n```\n{content}\n```"
                for path, content in list(context.source_files.items())[:15]
            )
        )

    # Stack-specific instructions
    if stack == "static_web":
        stack_instructions = (
            "### Technology Stack: Static Web (HTML/CSS/Vanilla JS)\n"
            "- This repository is a static frontend project (HTML, CSS, assets). There is NO backend, NO React, and NO package.json.\n"
            f"- Available source files in repo: {', '.join(f'`{k}`' for k in context.source_files.keys())}.\n"
            "- Generate a simple, focused test file (e.g. `tests/test_background.js` or `tests/test_background.py`) that directly inspects the relevant file (e.g. `style.css` or `index.html`) to assert that the ticket's acceptance criteria are met.\n"
            "- If using JavaScript, write clean Jest/Vitest test code reading the files via `fs.readFileSync(path.resolve(__dirname, '../style.css'), 'utf8')`.\n"
            "- STRICT PROHIBITIONS:\n"
            "  * DO NOT generate tests for `package.json`, `package-lock.json`, or node_modules.\n"
            "  * DO NOT generate tests verifying whether Node.js or npm is installed.\n"
            "  * DO NOT import React, ReactDOM, or `@testing-library/react`.\n"
            "  * DO NOT invent non-existent files or components like `App.tsx` or `styles.css` (use the exact file names present in the repo, such as `style.css`).\n"
        )
    elif stack == "react":
        stack_instructions = (
            "### Technology Stack: React\n"
            "- Use Jest / Vitest + React Testing Library.\n"
            "- Follow the naming convention: `<module>.test.tsx` or `<module>.test.ts`.\n"
            "- Test component behavior and UI elements specified in the acceptance criteria.\n"
            "- DO NOT generate irrelevant tests for package managers, lockfiles, or node installation.\n"
        )
    elif stack == "python":
        stack_instructions = (
            "### Technology Stack: Python\n"
            "- Use `pytest` for test cases.\n"
            "- Follow the naming convention: `tests/test_<feature>.py`.\n"
            "- Directly test functions, classes, or endpoints specified in the acceptance criteria.\n"
        )
    else:
        stack_instructions = (
            "### Technology Stack: Standard / Node\n"
            "- Use Jest / Vitest or the repo's existing test framework.\n"
            "- Follow the naming convention: `<module>.test.ts` or `<module>.test.js`.\n"
            "- DO NOT generate irrelevant tests for package managers or environment setup.\n"
        )

    prompt = (
        "You are a senior test engineer. Your task is to generate clean, highly relevant "
        "test cases based **solely** on the ticket's specification and acceptance criteria below.\n\n"
        "## Ticket\n"
        f"**ID:** {ticket.ticket_id}\n"
        f"**Summary:** {ticket.summary}\n"
        f"**Description:**\n{ticket.description}\n\n"
        f"**Acceptance Criteria:**\n{ticket.acceptance_criteria}\n\n"
        f"{source_summary}\n\n"
        f"{existing_tests_summary}\n\n"
        "## Stack & Testing Guidelines\n"
        f"{stack_instructions}\n"
        "## Core Principles\n"
        "1. Every single test MUST directly verify an acceptance criterion from the ticket.\n"
        "2. Keep test cases minimal, focused, and free of useless boilerplate.\n"
        "3. Do NOT test things unrelated to the ticket (e.g. do not test build configurations, lockfiles, or unrelated files).\n"
        "4. Follow the existing project structure and reference actual repository file paths.\n\n"
        "## Output Format\n"
        "Return your answer as one or more fenced code blocks, each preceded by a single-line file path comment like:\n"
        "```\n"
        "// FILE: tests/background.test.js\n"
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
        f"### `{path}`\n```\n{content}\n```"
        for path, content in current_tests.items()
    )

    prompt = (
        "A human reviewer has requested changes to the test cases you generated earlier.\n\n"
        f"## Reviewer Feedback\n{comments_text}\n\n"
        f"## Current Test Files\n{current_tests_text}\n\n"
        "## Instructions\n"
        "1. Address ALL points raised in the reviewer's feedback.\n"
        "2. If the reviewer requested removing any tests or test files, DO NOT include them.\n"
        "3. Return ONLY the complete, final set of valid test files that should exist in the repository.\n"
        "4. Do NOT generate unnecessary tests for package-lock.json, build tools, or node installation unless explicitly requested.\n"
        "5. Output format: fenced code blocks with `// FILE: path` comments.\n"
        "6. Do not include any explanation outside of code blocks."
    )

    response = llm.chat(prompt)
    content = response.get("content", "")
    revised = _parse_test_files(content)

    if revised:
        return revised

    logger.warning("LLM returned no parsed test files on revision; keeping existing tests.")
    return current_tests



def _parse_test_files(llm_output: str) -> dict[str, str]:
    """
    Parse the LLM's response into ``{file_path: content}`` pairs.

    Supports:
      - ``// FILE: path``, ``# FILE: path``, ``/* FILE: path */``
      - ``// filepath: path``, ``// filename: path``
      - Markdown headers like ``### `path` ``
      - Both fenced (```) and unfenced code outputs
    """
    files: dict[str, str] = {}
    lines = llm_output.splitlines()
    current_path: str | None = None
    current_content: list[str] = []

    prefixes = ("// FILE:", "# FILE:", "/* FILE:", "// filepath:", "// filename:", "# filepath:", "# filename:")

    for line in lines:
        stripped = line.strip()

        # Check for file marker
        is_marker = False
        target_path = None

        for prefix in prefixes:
            if stripped.lower().startswith(prefix.lower()):
                raw = stripped[len(prefix):].removesuffix("*/").strip().strip("`'\"")
                if raw:
                    is_marker = True
                    target_path = raw.replace("\\", "/")
                    break

        if not is_marker and (stripped.startswith("### `") or stripped.startswith("## `")) and stripped.endswith("`"):
            raw = stripped.lstrip("#").strip().strip("`'\"")
            if "." in raw:  # looks like a file path
                is_marker = True
                target_path = raw.replace("\\", "/")

        if is_marker and target_path:
            if current_path and current_content:
                files[current_path] = "\n".join(current_content).strip()
            current_path = target_path
            current_content = []
            continue

        if current_path is not None:
            # Skip code fence lines like ``` or ```typescript
            if stripped.startswith("```"):
                continue
            current_content.append(line)

    if current_path and current_content:
        files[current_path] = "\n".join(current_content).strip()

    return files


def _build_mr_description(
    ticket: Any,
    test_files: dict[str, str],
    ci_workflow_path: str | None = None,
) -> str:
    """Build the Gate-1 MR description body."""
    file_list = "\n".join(f"- `{path}`" for path in test_files)
    ci_note = f"\n- **CI Validation Workflow:** `{ci_workflow_path}`\n" if ci_workflow_path else ""
    return (
        f"## Spec-Driven Test Generation — Gate 1 Review\n\n"
        f"**Ticket:** {ticket.ticket_id} — {ticket.summary}\n\n"
        f"### Acceptance Criteria\n{ticket.acceptance_criteria}\n\n"
        f"### Generated Test Files\n{file_list}\n{ci_note}\n"
        f"---\n"
        f"Please review these tests for correctness and completeness "
        f"against the acceptance criteria.  **Approve this PR** to lock "
        f"the tests and proceed to automated code generation.\n\n"
        f"Any comments will be routed back to the AI agent for revision."
    )
