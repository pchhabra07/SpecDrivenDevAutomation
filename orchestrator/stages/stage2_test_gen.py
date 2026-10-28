"""
Stage 2 — Generate Test Cases (Pre-Development) & Repository CI Workflow.

Prompts the LLM to generate test cases driven by the ticket's
specification and acceptance criteria — *before* any code changes are
made.  Also ensures the repository has a repo-specific CI workflow
(generating one via LLM if none exists, or adapting an existing workflow
if needed for the new tests).

The proposed tests (and CI workflow if new/updated) are committed to a
dedicated branch and presented as a PR/MR for human review (Gate 1).

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
import time
from typing import Any

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.ci_generator import (
    find_existing_ci_workflow,
    generate_fallback_ci,
)
from orchestrator.exceptions import GatePendingError
from orchestrator.gitlab_gate import GateOutcome, check_approval_once, wait_for_approval
from orchestrator.llm_client import LLMClient
from orchestrator.preflight_checker import check_all, _check_yaml
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.tamper_check import build_lock_hashes

logger = logging.getLogger(__name__)

# Maximum Gate-1 review-revision cycles before aborting (default fallback).
_MAX_REVISION_CYCLES = 5


def run(
    context: TicketContext,
    gitlab: Any,
    llm: LLMClient,
    checkpoint: Checkpoint,
    audit: AuditLogger,
    gate_poll_interval: int = 30,
    gate_timeout_hours: float = 48.0,
    max_revision_cycles: int = _MAX_REVISION_CYCLES,
    test_preflight_max_repairs: int = 2,
    blocking_gate: bool = True,
    checkpoint_dir: str = "",
) -> dict[str, str]:
    """
    Execute Stage 2: generate tests & workflow → open PR → await approval → lock.

    Parameters
    ----------
    context : TicketContext
        Structured ticket data, paths, and source files.
    gitlab : Any
        GitLabClient or GitHubClient instance.
    llm : LLMClient
        LLM gateway.
    checkpoint : Checkpoint
        Persistent pipeline checkpoint.
    audit : AuditLogger
        Audit event logger.
    gate_poll_interval : int
        Polling interval in seconds for blocking gate mode.
    gate_timeout_hours : float
        Maximum wait time in hours before gate times out in blocking mode.
    max_revision_cycles : int
        Maximum Gate 1 review/revision cycles before pipeline aborts.
    test_preflight_max_repairs : int
        Maximum attempts for LLM syntax repair of generated tests.
    blocking_gate : bool
        If True, blocks synchronously in a polling loop (CLI/dev mode).
        If False, checks once and raises GatePendingError if unresolved (server/webhook mode).
    checkpoint_dir : str
        Directory to save checkpoints when suspending execution.

    Returns
    -------
    dict[str, str]
        ``{file_path: content}`` of the approved & locked test files.
    """
    audit.log(stage=2, event_type="stage2_start")
    ticket = context.ticket

    # Early exit if Gate 1 already approved
    if checkpoint.gate1_approved and checkpoint.locked_test_hashes:
        logger.info("Gate 1 already approved for ticket %s.", ticket.ticket_id)
        if checkpoint.generated_test_files:
            return checkpoint.generated_test_files
        return {}

    tests_branch = f"automation/{ticket.ticket_id}/tests"
    checkpoint.tests_branch = tests_branch

    # Detect stack and log audit event (S2-3)
    ticket_hint = f"{ticket.summary} {ticket.description} {ticket.acceptance_criteria}"
    stack = _detect_project_stack(context, ticket_hint=ticket_hint)
    audit.log(
        stage=2,
        event_type="stack_detected",
        payload={"stack": stack, "ticket_id": ticket.ticket_id},
    )

    # ── 1 & 2 & 3: Check if tests already generated and branch/PR already opened ──
    if checkpoint.gate1_mr_iid is None and hasattr(gitlab, "_repo"):
        try:
            open_pulls = list(gitlab._repo.get_pulls(state="open"))
            matched = [p for p in open_pulls if p.head.ref == tests_branch]
            if matched:
                checkpoint.gate1_mr_iid = matched[0].number
                logger.info(
                    "Discovered existing open Gate 1 PR #%d for branch %s",
                    checkpoint.gate1_mr_iid,
                    tests_branch,
                )
                if checkpoint_dir:
                    checkpoint.save(checkpoint_dir)
        except Exception as exc:
            logger.debug("Could not inspect existing PRs: %s", exc)

    if checkpoint.gate1_mr_iid is not None and checkpoint.generated_test_files:
        logger.info(
            "Reusing existing Gate 1 PR #%d and generated tests from checkpoint",
            checkpoint.gate1_mr_iid,
        )
        generated_tests = dict(checkpoint.generated_test_files)
        approved_tests = generated_tests
    else:
        # Create the tests-only branch
        logger.info("Creating test branch %s…", tests_branch)
        if not gitlab.branch_exists(tests_branch):
            gitlab.create_branch(tests_branch, ref=context.default_branch)

        # Inspect for existing CI workflow on default branch / repository
        existing_ci_path, existing_ci_content = find_existing_ci_workflow(
            gitlab=gitlab,
            branch=context.default_branch,
            all_file_paths=context.all_file_paths,
        )

        repo_id = getattr(gitlab, "project_id", None) or getattr(gitlab, "repo_name", "target-repo")
        generated_tests, ci_workflow_update = _generate_tests(
            llm=llm,
            context=context,
            existing_ci_path=existing_ci_path,
            existing_ci_content=existing_ci_content,
            repo_id=str(repo_id),
        )

        if not generated_tests:
            logger.error("LLM returned no test files for ticket %s", ticket.ticket_id)
            raise RuntimeError(
                f"Stage 2 failed: LLM did not generate any test files matching the expected format for ticket {ticket.ticket_id}."
            )

        # S2-5: Preflight syntax checks & repair on generated tests before committing
        generated_tests = _preflight_and_repair_tests(
            llm=llm,
            tests=generated_tests,
            max_repairs=test_preflight_max_repairs,
            audit=audit,
        )

        # Determine final CI workflow path
        ci_created_or_updated = False
        if ci_workflow_update:
            ci_workflow_path, ci_workflow_content = ci_workflow_update
            ci_created_or_updated = True
        elif existing_ci_path:
            ci_workflow_path = existing_ci_path
            ci_workflow_content = None
        else:
            ci_workflow_path, ci_workflow_content = generate_fallback_ci(str(repo_id), stack=stack)
            ci_created_or_updated = True

        checkpoint.ci_workflow_path = ci_workflow_path

        # Commit test files & CI workflow
        files_to_commit = dict(generated_tests)
        if ci_created_or_updated and ci_workflow_content:
            files_to_commit[ci_workflow_path] = ci_workflow_content

        logger.info(
            "Committing %d file(s) (tests%s) to branch %s",
            len(files_to_commit),
            " + CI workflow" if (ci_created_or_updated and ci_workflow_content) else "",
            tests_branch,
        )
        gitlab.commit_files(
            branch=tests_branch,
            files=files_to_commit,
            commit_message=f"[spec-driven] Generate tests for {ticket.ticket_id}",
        )
        checkpoint.generated_test_files = generated_tests

        if checkpoint.gate1_mr_iid is None:
            logger.info("Opening draft PR for Gate 1 review…")
            mr_info = gitlab.create_mr(
                source_branch=tests_branch,
                target_branch=context.default_branch,
                title=f"[Gate 1] Tests for {ticket.ticket_id}: {ticket.summary}",
                description=_build_mr_description(
                    ticket,
                    generated_tests,
                    ci_workflow_path=ci_workflow_path if ci_created_or_updated else None,
                ),
                draft=True,
            )
            checkpoint.gate1_mr_iid = mr_info.iid
            audit.log(stage=2, event_type="gate1_opened", payload={"mr_iid": mr_info.iid})
            if checkpoint_dir:
                checkpoint.save(checkpoint_dir)
        approved_tests = generated_tests

    # ── 5. Wait or check for human approval ───────────────────────────
    seen_comments: set[str] = set()
    if hasattr(gitlab, "get_pr_notes"):
        for n in gitlab.get_pr_notes(checkpoint.gate1_mr_iid):
            b = (n.get("body") or "").strip()
            if b:
                seen_comments.add(b)
    elif hasattr(gitlab, "get_mr_notes"):
        for n in gitlab.get_mr_notes(checkpoint.gate1_mr_iid):
            b = (n.get("body") or "").strip()
            if b:
                seen_comments.add(b)

    # ── NON-BLOCKING GATE (S2-1) ──────────────────────────────────────
    if not blocking_gate:
        logger.info(
            "Checking Gate 1 approval state (non-blocking) for PR #%d…",
            checkpoint.gate1_mr_iid,
        )
        gate_result = check_approval_once(
            gitlab=gitlab,
            mr_iid=checkpoint.gate1_mr_iid,
            seen_comments=seen_comments,
        )

        if gate_result.outcome == GateOutcome.PENDING:
            checkpoint.gate_waiting = "gate1"
            checkpoint.gate_waiting_mr_iid = checkpoint.gate1_mr_iid
            if not checkpoint.gate_waiting_since:
                checkpoint.gate_waiting_since = time.time()
            checkpoint.current_stage = 2
            if checkpoint_dir:
                checkpoint.save(checkpoint_dir)
            logger.info(
                "Gate 1 pending for PR #%d. Pipeline suspended (non-blocking). Re-trigger when reviewed.",
                checkpoint.gate1_mr_iid,
            )
            raise GatePendingError("gate1", checkpoint.gate1_mr_iid)

        elif gate_result.outcome == GateOutcome.CHANGES_REQUESTED:
            checkpoint.gate1_revision_count += 1
            cycle = checkpoint.gate1_revision_count
            logger.info(
                "Reviewer requested changes (cycle %d/%d, %d comments). Re-generating tests.",
                cycle, max_revision_cycles, len(gate_result.reviewer_comments),
            )
            if cycle > max_revision_cycles:
                raise RuntimeError(
                    f"Gate 1 for {ticket.ticket_id} not approved after {max_revision_cycles} revision cycles."
                )

            audit.log(
                stage=2,
                event_type="gate1_changes_requested",
                payload={"comments": gate_result.reviewer_comments, "cycle": cycle},
            )
            last_ci_output = getattr(gate_result, "ci_output", "") or ""
            revised_tests, revised_ci = _revise_tests(
                llm, context, approved_tests, gate_result.reviewer_comments,
                ci_failure_output=last_ci_output,
                gitlab=gitlab,
                tests_branch=tests_branch,  # Fix 3 (ContextIssue)
            )

            # S2-5: Preflight repair on revised tests
            revised_tests = _preflight_and_repair_tests(
                llm=llm,
                tests=revised_tests,
                max_repairs=test_preflight_max_repairs,
                audit=audit,
            )

            # Remove obsolete files from branch
            removed_files = set(approved_tests.keys()) - set(revised_tests.keys())
            for rm_path in removed_files:
                if hasattr(gitlab, "delete_file"):
                    gitlab.delete_file(
                        path=rm_path,
                        branch=tests_branch,
                        message=f"[spec-driven] Remove obsolete test {rm_path} per reviewer feedback",
                    )

            approved_tests = revised_tests
            checkpoint.generated_test_files = approved_tests
            revision_commit_files = dict(approved_tests)
            if revised_ci:
                revision_commit_files[revised_ci[0]] = revised_ci[1]

            gitlab.commit_files(
                branch=tests_branch,
                files=revision_commit_files,
                commit_message=(
                    f"[spec-driven] Revise tests for {ticket.ticket_id} (revision {cycle})"
                ),
            )

            file_bullets = "\n".join(f"- `{f}`" for f in approved_tests)
            removed_bullets = (
                f"\n\n**Removed Files:**\n" + "\n".join(f"- `{f}`" for f in removed_files)
                if removed_files
                else ""
            )
            bot_msg = (
                f"<!-- spec-driven-bot -->\n"
                f"🤖 **Spec-Driven Dev Orchestrator** (Revision {cycle}/{max_revision_cycles})\n\n"
                f"I have updated the test suite based on your feedback.\n\n"
                f"**Active Test Files:**\n{file_bullets}{removed_bullets}\n\n"
                f"Please review the updated tests. If everything looks good, approve this PR to proceed."
            )
            if hasattr(gitlab, "add_mr_comment"):
                gitlab.add_mr_comment(checkpoint.gate1_mr_iid, bot_msg)
            elif hasattr(gitlab, "add_pr_comment"):
                gitlab.add_pr_comment(checkpoint.gate1_mr_iid, bot_msg)

            checkpoint.gate_waiting = "gate1"
            checkpoint.gate_waiting_mr_iid = checkpoint.gate1_mr_iid
            if not checkpoint.gate_waiting_since:
                checkpoint.gate_waiting_since = time.time()
            checkpoint.current_stage = 2
            if checkpoint_dir:
                checkpoint.save(checkpoint_dir)
            logger.info(
                "Revised tests pushed for PR #%d (cycle %d). Suspending until human review.",
                checkpoint.gate1_mr_iid, cycle,
            )
            raise GatePendingError("gate1", checkpoint.gate1_mr_iid)

        elif gate_result.outcome == GateOutcome.APPROVED:
            logger.info("Gate 1 APPROVED (non-blocking) for %s", ticket.ticket_id)
            checkpoint.gate_waiting = None
            checkpoint.gate_waiting_mr_iid = None
            checkpoint.gate_waiting_since = None
            audit.log(
                stage=2,
                event_type="gate1_approved",
                payload={"approved_by": gate_result.approved_by, "cycle": checkpoint.gate1_revision_count},
            )

    # ── BLOCKING GATE (Existing sync loop) ─────────────────────────────
    else:
        logger.info("Waiting for Gate 1 human approval (blocking mode)…")
        for cycle in range(1, max_revision_cycles + 1):
            logger.info("Gate 1 approval cycle %d/%d", cycle, max_revision_cycles)
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
                checkpoint.gate_waiting = None
                checkpoint.gate_waiting_mr_iid = None
                checkpoint.gate_waiting_since = None
                audit.log(
                    stage=2,
                    event_type="gate1_approved",
                    payload={"approved_by": gate_result.approved_by, "cycle": cycle},
                )
                break

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
                last_ci_output = getattr(gate_result, "ci_output", "") or ""
                revised_tests, revised_ci = _revise_tests(
                    llm, context, approved_tests, gate_result.reviewer_comments,
                    ci_failure_output=last_ci_output,
                    gitlab=gitlab,
                    tests_branch=tests_branch,  # Fix 3 (ContextIssue)
                )

                # S2-5: Preflight repair on revised tests
                revised_tests = _preflight_and_repair_tests(
                    llm=llm,
                    tests=revised_tests,
                    max_repairs=test_preflight_max_repairs,
                    audit=audit,
                )

                removed_files = set(approved_tests.keys()) - set(revised_tests.keys())
                for rm_path in removed_files:
                    if hasattr(gitlab, "delete_file"):
                        gitlab.delete_file(
                            path=rm_path,
                            branch=tests_branch,
                            message=f"[spec-driven] Remove obsolete test {rm_path} per reviewer feedback",
                        )

                approved_tests = revised_tests
                checkpoint.generated_test_files = approved_tests
                revision_commit_files = dict(approved_tests)
                if revised_ci:
                    revision_commit_files[revised_ci[0]] = revised_ci[1]

                gitlab.commit_files(
                    branch=tests_branch,
                    files=revision_commit_files,
                    commit_message=(
                        f"[spec-driven] Revise tests for {ticket.ticket_id} (revision {cycle})"
                    ),
                )

                file_bullets = "\n".join(f"- `{f}`" for f in approved_tests)
                removed_bullets = (
                    f"\n\n**Removed Files:**\n" + "\n".join(f"- `{f}`" for f in removed_files)
                    if removed_files
                    else ""
                )
                bot_msg = (
                    f"<!-- spec-driven-bot -->\n"
                    f"🤖 **Spec-Driven Dev Orchestrator** (Revision {cycle}/{max_revision_cycles})\n\n"
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
                f"Gate 1 for {ticket.ticket_id} not approved after {max_revision_cycles} revision cycles."
            )

    # ── 6. Pre-lock symbol validation (Issue 5 fix) ───────────────────
    validation_warnings = _validate_test_imports(approved_tests, context)
    if validation_warnings:
        logger.warning(
            "Pre-lock validation found %d potential issue(s) in test files:\n%s",
            len(validation_warnings),
            "\n".join(f"  - {w}" for w in validation_warnings),
        )

    # ── 7. Lock test files ──────────────────────────────────────────────
    locked_hashes = build_lock_hashes(approved_tests)
    checkpoint.locked_test_hashes = locked_hashes
    checkpoint.gate1_approved = True
    checkpoint.current_stage = 3
    checkpoint.gate_waiting = None
    checkpoint.gate_waiting_mr_iid = None
    checkpoint.gate_waiting_since = None

    audit.log(
        stage=2,
        event_type="tests_locked",
        payload={"locked_files": list(locked_hashes.keys())},
    )
    logger.info("Stage 2 complete — %d test file(s) locked.", len(locked_hashes))
    return approved_tests


# ── Project stack detection helper ─────────────────────────────────────


def _detect_project_stack(context: TicketContext, ticket_hint: str = "") -> str:
    """
    Detect which layer of the stack is RELEVANT TO THIS TICKET.

    Tier 0 (S2-3): Authoritative label override from ticket (e.g. stack:python, stack:react).
    Tier 1 (S2-3): Inspect file CONTENTS of selected source files for framework markers.
    Tier 2: Check selected source file extensions.
    Tier 3: Ticket text hints.
    Tier 4: Repo-wide fallback file tree.
    """
    # ── Tier 0: Check ticket labels for authoritative stack override ──────
    stack_labels = {
        "stack:python": "python",
        "stack:react": "react",
        "stack:node": "node",
        "stack:javascript": "node",
        "stack:js": "node",
        "stack:typescript": "node",
        "stack:ts": "node",
        "stack:java": "java",
        "stack:go": "go",
        "stack:golang": "go",
        "stack:rust": "rust",
        "stack:static_web": "static_web",
        "stack:web": "static_web",
        "stack:html": "static_web",
    }
    for label in (context.ticket.labels or []):
        normalized_label = label.strip().lower()
        if normalized_label in stack_labels:
            detected = stack_labels[normalized_label]
            logger.info("Stack explicitly set via ticket label '%s': %s", label, detected)
            return detected

    # ── Tier 1: Check file CONTENTS of selected source files ─────────────
    # Inspect content of up to 10 selected source files to detect frameworks
    for path, content in list(context.source_files.items())[:10]:
        p_lower = path.lower()
        c_lower = content.lower()
        if p_lower.endswith((".tsx", ".jsx", ".ts", ".js")):
            if any(k in c_lower for k in ("from 'react'", 'from "react"', "from react", "import react", "react.createelement", "usestate")):
                return "react"
        if p_lower.endswith(".py"):
            if any(k in c_lower for k in ("import pytest", "import unittest", "from django", "from fastapi", "from flask")):
                return "python"
        if p_lower.endswith(".go"):
            if "package " in c_lower and ("import (" in c_lower or 'import "' in c_lower):
                return "go"
        if p_lower.endswith(".rs"):
            if "use std::" in c_lower or "fn main(" in c_lower or "pub fn " in c_lower:
                return "rust"
        if p_lower.endswith(".java"):
            if "@springbootapplication" in c_lower or "import org.springframework" in c_lower or "package " in c_lower:
                return "java"

    paths = [p.lower() for p in context.all_file_paths]
    ticket_lower = ticket_hint.lower()
    selected = [p.lower() for p in context.source_files.keys()]

    # ── Tier 2: check what the SELECTED source files actually are ────────
    selected_has_web   = any(p.endswith((".css", ".scss", ".html", ".htm")) for p in selected)
    selected_has_react = any(p.endswith((".tsx", ".jsx")) or "react" in p for p in selected)
    selected_has_js    = any(p.endswith((".ts", ".js")) for p in selected)
    selected_has_py    = any(p.endswith(".py") for p in selected)
    selected_has_java  = any(p.endswith(".java") for p in selected)
    selected_has_go    = any(p.endswith(".go") for p in selected)
    selected_has_rust  = any(p.endswith(".rs") for p in selected)

    if selected_has_react:
        return "react"
    if selected_has_web and not selected_has_py and not selected_has_java:
        return "static_web"
    if selected_has_js and not selected_has_py:
        return "node"
    if selected_has_go:
        return "go"
    if selected_has_rust:
        return "rust"
    if selected_has_java:
        return "java"
    if selected_has_py:
        return "python"

    # ── Tier 3: ticket text hints (e.g. "CSS dark mode" → static_web) ──
    frontend_keywords = ("css", "html", "style", "color", "layout", "ui ", "frontend", "dark mode", "responsive")
    react_keywords    = ("react", "component", "tsx", "jsx", "hook", "props", "state", "render")
    if any(k in ticket_lower for k in react_keywords):
        return "react"
    if any(k in ticket_lower for k in frontend_keywords):
        has_pkg_json = any(p == "package.json" or p.endswith("/package.json") for p in paths)
        return "node" if has_pkg_json else "static_web"

    # ── Tier 4: repo-wide fallback (original priority queue) ────────────
    has_python = any(
        p.endswith(".py") or p in ("pyproject.toml", "requirements.txt", "setup.py", "pipfile")
        for p in paths
    )
    has_pkg_json = any(p == "package.json" or p.endswith("/package.json") for p in paths)
    if has_pkg_json:
        has_react = any(p.endswith((".tsx", ".jsx")) or "react" in p for p in paths)
        return "react" if has_react else "node"
    if has_python:
        return "python"
    has_java = any(p in ("pom.xml", "build.gradle", "gradlew") or p.endswith(".java") for p in paths)
    if has_java:
        return "java"
    has_go = any(p in ("go.mod", "go.sum") or p.endswith(".go") for p in paths)
    if has_go:
        return "go"
    has_rust = any(p == "cargo.toml" or p.endswith(".rs") for p in paths)
    if has_rust:
        return "rust"
    has_html_css = any(p.endswith((".html", ".htm", ".css")) for p in paths)
    if has_html_css:
        return "static_web"
    return "generic"


# ── LLM prompt helpers ──────────────────────────────────────────────────


def _get_stack_instructions(stack: str, context: TicketContext) -> str:
    """Return stack-specific test generation guidelines."""
    if stack == "python":
        return (
            "### Technology Stack: Python\n"
            "- Use `pytest` for test cases.\n"
            "- Follow naming convention: `tests/test_<feature>.py`.\n"
            "- Directly test functions, classes, or endpoints specified in the acceptance criteria.\n"
            "- Assert specific expected return values, schemas, error exceptions, and side-effects.\n"
            "- STRICTLY FORBIDDEN: Writing trivial tests like `assert result is not None`, `assert True`, or mocking away the logic being tested.\n"
            "- DO NOT import JavaScript, Jest, React, or npm modules.\n"
        )
    elif stack == "react":
        return (
            "### Technology Stack: React\n"
            "- Use Jest / Vitest + React Testing Library.\n"
            "- Follow naming convention: `<module>.test.tsx` or `<module>.test.ts`.\n"
            "- Test component behavior, user events (clicks, typing), props, and rendered UI elements.\n"
            "- Assert real visible text, rendered data, and state updates matching acceptance criteria.\n"
            "- STRICTLY FORBIDDEN: Shallow snapshot tests or tests that merely verify an empty wrapper div rendered.\n"
            "- DO NOT generate irrelevant tests for package managers or environment setup.\n"
        )
    elif stack == "node":
        return (
            "### Technology Stack: Node.js / JavaScript\n"
            "- Use Jest / Vitest or the repo's existing test framework.\n"
            "- Follow naming convention: `<module>.test.ts` or `<module>.test.js`.\n"
            "- Directly test business logic, routes, and data transformations.\n"
            "- Assert exact payloads, HTTP status codes, and error conditions.\n"
            "- STRICTLY FORBIDDEN: Trivial tests that merely check if an endpoint exists without verifying its returned data.\n"
            "- DO NOT generate irrelevant tests for package managers or environment setup.\n"
        )
    elif stack == "java":
        return (
            "### Technology Stack: Java\n"
            "- Use JUnit 5 for test cases.\n"
            "- Follow standard Maven/Gradle naming: `src/test/java/.../*Test.java`.\n"
            "- Assert explicit business logic, return values, and contract requirements.\n"
            "- STRICTLY FORBIDDEN: Dummy assertions like `assertTrue(true)` or `assertNotNull(obj)` without value checks.\n"
        )
    elif stack == "go":
        return (
            "### Technology Stack: Go\n"
            "- Use standard Go `testing` package.\n"
            "- Follow naming convention: `<package>_test.go`.\n"
            "- Assert concrete outputs, error types, and boundary conditions.\n"
        )
    elif stack == "rust":
        return (
            "### Technology Stack: Rust\n"
            "- Use standard Rust test modules (`#[cfg(test)]`) or `tests/<feature>_test.rs`.\n"
            "- Assert explicit state mutations, return values, and error variants with strict `assert_eq!`.\n"
        )
    elif stack == "static_web":
        return (
            "### Technology Stack: Static Web (HTML/CSS/Vanilla JS)\n"
            "- This repository is a static frontend project (HTML, CSS, assets). There is NO backend and NO package.json.\n"
            f"- Available source files in repo: {', '.join(f'`{k}`' for k in context.source_files.keys())}.\n"
            "- Generate a simple, focused test file (e.g. `tests/test_ui.py` with BeautifulSoup) that inspects DOM/CSS to assert that acceptance criteria are met.\n"
            "- CRITICAL TESTING RULES FOR UI/CSS:\n"
            "  * NEVER write tests that only check for the presence of a class name (e.g. class='dark-mode').\n"
            "  * ALWAYS assert actual CSS properties (e.g. background-color, color, contrast) or stylesheet rules.\n"
            "  * ALWAYS assert that existing page content, sections, and structural elements remain present and intact.\n"
            "  * MANDATORY: Every test function MUST have at least one unconditional `assert` statement.\n"
            "    Tests that ONLY assert inside `if 'prop' in style:` guards are FORBIDDEN — they silently pass on unchanged code:\n"
            "    # WRONG (silently passes if property absent):\n"
            "    if 'background-color' in style: assert style['background-color'] == '#1a1a1a'\n"
            "    # CORRECT (fails loudly if property absent):\n"
            "    assert 'background-color' in style, 'background-color must be explicitly set'\n"
            "    assert style['background-color'] == '#1a1a1a'\n"
            "  * MANDATORY: Use absolute file paths for all file reads. Use:\n"
            "    `os.path.join(os.path.dirname(__file__), '..', 'style.css')` NOT `open('style.css')`.\n"
            "  * MANDATORY: Extract expected CSS values from the ACTUAL source files provided above.\n"
            "    Do NOT hardcode values from memory. Check shorthand properties (e.g. `border-bottom: 1px solid #f2f2f2`)\n"
            "    vs longhand (`border-bottom-color`) — cssutils resolves these differently.\n"
            "- STRICT PROHIBITIONS:\n"
            "  * DO NOT generate tests for `package.json`, `package-lock.json`, or node_modules.\n"
            "  * DO NOT import React, ReactDOM, or `@testing-library/react`.\n"
            "  * DO NOT invent non-existent files or components (use the exact file names present in the repo).\n"
        )
    return (
        "### Technology Stack: Multi-Language / Generic\n"
        "- Inspect the existing repository files and test frameworks.\n"
        "- Write tests matching the repository's native conventions.\n"
        "- Assert real functional outputs and data transformations; never write dummy assertions.\n"
    )


def _build_test_prompt(context: TicketContext, stack: str) -> str:
    """Build the prompt for generating test cases only (S2-6)."""
    ticket = context.ticket

    existing_tests_summary = ""
    if context.test_files:
        existing_tests_summary = (
            "## Existing Test Files\n"
            + "\n".join(
                f"### `{path}`\n```\n{content}\n```"
                for path, content in list(context.test_files.items())[:10]
            )
        )

    source_summary = ""
    if context.source_files:
        seen: set[str] = set()
        priority_paths: list[str] = []
        for p in (context.llm_selected_paths or []):
            if p in context.source_files and p not in seen:
                priority_paths.append(p)
                seen.add(p)
        for p in context.source_files:
            if p not in seen:
                priority_paths.append(p)
                seen.add(p)

        source_summary = (
            "## Relevant Source Files\n"
            + "\n".join(
                f"### `{path}`\n```\n{context.source_files[path]}\n```"
                for path in priority_paths[:15]
            )
        )

    stack_instructions = _get_stack_instructions(stack, context)

    return (
        "You are a senior test and automation engineer. Your task is to generate clean, highly relevant "
        "test cases based **solely** on the ticket's specification and acceptance criteria below.\n\n"
        "## Ticket\n"
        f"**ID:** {ticket.ticket_id}\n"
        f"**Summary:** {ticket.summary}\n"
        f"**Description:**\n{ticket.description}\n\n"
        f"**Acceptance Criteria:**\n{ticket.acceptance_criteria}\n\n"
        f"{source_summary}\n\n"
        f"{existing_tests_summary}\n\n"
        "## Stack & Testing Guidelines\n"
        f"{stack_instructions}\n\n"
        "## Core Principles (MANDATORY ACROSS ALL TECH STACKS)\n"
        "1. Every single test MUST directly verify an acceptance criterion from the ticket.\n"
        "2. REAL FUNCTIONAL ASSERTIONS (ZERO TOLERANCE FOR SUPERFICIAL TESTS):\n"
        "   - Assert exact return values, response payloads, state changes, computed styles, or DOM hierarchy.\n"
        "   - STRICTLY FORBIDDEN: Writing trivial assertions that pass without real implementation (e.g. `assert obj is not None`, `assertTrue(true)`, checking only a class name or tag without checking styles/content, or over-mocking the very unit under test).\n"
        "   - N3 MANDATORY: Every test function MUST include at least ONE unconditional `assert` statement. Tests that ONLY assert inside `if 'prop' in style:` guards are FORBIDDEN -- they silently pass on unchanged code. WRONG: `if 'background-color' in s: assert s['background-color'] == '#111'`. CORRECT: `assert 'background-color' in s; assert s['background-color'] == '#111'`\n"
        "3. REGRESSION CHECKS: Assert that existing core components, layouts, APIs, and structures remain intact after changes are introduced.\n"
        "4. EDGE CASES & ERROR PATHS: Include tests for invalid inputs, missing data, or boundary conditions if applicable.\n"
        "5. Keep test cases minimal, focused, and free of useless boilerplate.\n"
        "6. Do NOT test things unrelated to the ticket (e.g. do not test build configurations or lockfiles).\n"
        "7. Follow the existing project structure and reference actual repository file paths.\n\n"
        "## Output Format\n"
        "Return your answer as one or more fenced code blocks, each preceded by a single-line file path comment like:\n"
        "```\n"
        "// FILE: tests/my_test.py\n"
        "```\n"
        "followed by the complete file content.\n"
        "Do not include any explanation outside of code blocks."
    )


def _build_ci_prompt(
    context: TicketContext,
    repo_id: str,
    stack: str,
    generated_tests: dict[str, str],
) -> str:
    """Build the prompt for generating a new CI workflow (S2-6)."""
    test_files_list = "\n".join(f"- `{path}`" for path in generated_tests)
    return (
        "You are a DevOps and CI/CD automation engineer. Generate a complete, working GitHub Actions workflow file "
        f"tailored specifically to execute test cases for this repository (project: {repo_id}, stack: {stack}).\n\n"
        f"## Active Test Files to Execute\n{test_files_list}\n\n"
        "## Workflow Requirements\n"
        "1. File path: `// FILE: .github/workflows/ci.yml`\n"
        "2. `runs-on: ubuntu-latest`\n"
        "3. Triggers:\n"
        "   ```yaml\n"
        "   on:\n"
        "     push:\n"
        "       branches: ['automation/**']\n"
        "     workflow_dispatch:\n"
        "       inputs:\n"
        "         test_only:\n"
        "           description: 'Run test job only'\n"
        "           required: false\n"
        "           default: 'false'\n"
        "   ```\n"
        "4. Concurrency:\n"
        "   ```yaml\n"
        "   concurrency:\n"
        "     group: ${{ github.workflow }}-${{ github.ref }}\n"
        "     cancel-in-progress: true\n"
        "   ```\n"
        f"5. Steps: checkout (`actions/checkout@v4`), setup appropriate runtime for {stack}, "
        "install dependencies, and execute the generated test files.\n\n"
        "## Output Format\n"
        "Return the complete YAML workflow in a single fenced code block preceded by `// FILE: .github/workflows/ci.yml`.\n"
        "Do not include explanation outside code blocks."
    )


def _build_ci_update_prompt(
    context: TicketContext,
    existing_ci_path: str,
    existing_ci_content: str,
    generated_tests: dict[str, str],
) -> str:
    """Build the prompt for evaluating and updating an existing CI workflow (S2-6)."""
    test_files_list = "\n".join(f"- `{path}`" for path in generated_tests)
    return (
        "You are a DevOps and CI/CD automation engineer. Evaluate whether the existing CI/CD workflow needs updates "
        "to run the newly generated test cases.\n\n"
        f"## Existing CI/CD Workflow (`{existing_ci_path}`)\n"
        f"```yaml\n{existing_ci_content}\n```\n\n"
        f"## Generated Test Files\n{test_files_list}\n\n"
        "If updates ARE needed (e.g. adding test execution steps or dependencies):\n"
        f"Return the complete, updated workflow preceded by `// FILE: {existing_ci_path}`.\n"
        "If NO updates are needed: Return nothing (empty output)."
    )


def _preflight_and_repair_tests(
    llm: LLMClient,
    tests: dict[str, str],
    max_repairs: int,
    audit: AuditLogger,
) -> dict[str, str]:
    """
    S2-5 FIX: Validate generated test files before commit using preflight syntax checks.
    If syntax errors are detected, invoke LLM repair loop with stateless chat_once()
    up to max_repairs attempts.
    """
    if not tests:
        return tests

    current_tests = dict(tests)
    for attempt in range(1, max_repairs + 1):
        passed, results = check_all(current_tests)
        if passed:
            return current_tests

        failed = [r for r in results if not r.passed]
        failed_paths = [r.path for r in failed]
        logger.warning(
            "Generated tests failed preflight syntax check (attempt %d/%d): %s",
            attempt,
            max_repairs,
            failed_paths,
        )
        audit.log(
            stage=2,
            event_type="test_preflight_failed",
            payload={"attempt": attempt, "failed_files": failed_paths, "errors": [r.errors for r in failed]},
        )

        repair_prompt_lines = [
            "Your generated test files contain syntax errors caught by pre-flight verification.",
            "Fix ALL of the following errors. Return the COMPLETE, corrected test file(s).",
            "MANDATORY OUTPUT FORMAT: Output each repaired file in a fenced code block preceded by `// FILE: <path>`.",
            "Do not include any commentary outside of code blocks.",
            "",
        ]
        for r in failed:
            repair_prompt_lines.append(f"### File: `{r.path}` ({r.language})")
            for err in r.errors:
                repair_prompt_lines.append(f"- Error: {err}")
            repair_prompt_lines.append("Current code:")
            repair_prompt_lines.append("```")
            repair_prompt_lines.append(current_tests.get(r.path, ""))
            repair_prompt_lines.append("```")
            repair_prompt_lines.append("")

        repair_prompt = "\n".join(repair_prompt_lines)
        repair_response = llm.chat_once(
            repair_prompt,
            stage=2,
            call_purpose=f"Repair test syntax errors (attempt {attempt}/{max_repairs})",
        )
        repaired_parsed = _parse_test_files(repair_response.get("content", ""))

        applied_files: list[str] = []
        for path, new_content in repaired_parsed.items():
            norm_path = path.replace("\\", "/")
            if norm_path in current_tests:
                # Validate the single repaired file
                file_passed, _ = check_all({norm_path: new_content})
                if file_passed:
                    current_tests[norm_path] = new_content
                    applied_files.append(norm_path)
                else:
                    logger.warning("Repaired test file %s still has syntax errors.", norm_path)

        if applied_files:
            audit.log(
                stage=2,
                event_type="test_preflight_repaired",
                payload={"attempt": attempt, "repaired_files": applied_files},
            )
            logger.info("Preflight repair applied to %d test file(s).", len(applied_files))

    # Final check
    final_passed, _ = check_all(current_tests)
    if not final_passed:
        logger.warning(
            "Generated tests still contain preflight syntax errors after %d repair attempt(s). Proceeding.",
            max_repairs,
        )
    return current_tests


def _generate_tests(
    llm: LLMClient,
    context: TicketContext,
    existing_ci_path: str | None = None,
    existing_ci_content: str | None = None,
    repo_id: str = "target-repo",
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """
    Prompt the LLM to generate test cases and appropriate CI workflow from the ticket spec.
    Splits test generation and CI generation into distinct prompts (S2-6).
    """
    ticket = context.ticket
    ticket_hint = f"{ticket.summary} {ticket.description} {ticket.acceptance_criteria}"
    stack = _detect_project_stack(context, ticket_hint=ticket_hint)

    # 1. Call 1: Generate tests
    test_prompt = _build_test_prompt(context, stack=stack)
    response = llm.chat(
        test_prompt,
        stage=2,
        call_purpose="Generate pre-development tests",
        attached_files=list(context.source_files.keys()),
    )
    content = response.get("content", "")
    all_files = _parse_test_files(content)

    test_files: dict[str, str] = {}
    ci_workflow: tuple[str, str] | None = None

    for path, file_content in all_files.items():
        norm = path.replace("\\", "/")
        if norm.startswith(".github/workflows/") or norm == ".gitlab-ci.yml":
            ci_workflow = (norm, file_content)
        else:
            test_files[norm] = file_content

    # 2. Call 2: Generate/update CI workflow if not already bundled in Call 1 (S2-6)
    if ci_workflow is None:
        if not existing_ci_path:
            ci_prompt = _build_ci_prompt(
                context, repo_id=repo_id, stack=stack, generated_tests=test_files
            )
            ci_response = llm.chat_once(
                ci_prompt,
                stage=2,
                call_purpose="Generate repository CI workflow (stateless)",
            )
            ci_files = _parse_test_files(ci_response.get("content", ""))
            for path, file_content in ci_files.items():
                norm = path.replace("\\", "/")
                if norm.startswith(".github/workflows/") or norm == ".gitlab-ci.yml":
                    ci_workflow = (norm, file_content)
                    break
        elif existing_ci_path and existing_ci_content:
            ci_update_prompt = _build_ci_update_prompt(
                context,
                existing_ci_path=existing_ci_path,
                existing_ci_content=existing_ci_content,
                generated_tests=test_files,
            )
            ci_response = llm.chat_once(
                ci_update_prompt,
                stage=2,
                call_purpose="Update existing CI workflow (stateless)",
            )
            ci_files = _parse_test_files(ci_response.get("content", ""))
            for path, file_content in ci_files.items():
                norm = path.replace("\\", "/")
                if norm == existing_ci_path.replace("\\", "/"):
                    ci_workflow = (norm, file_content)
                    break

    # 3. Validate CI workflow YAML if present
    if ci_workflow:
        ci_path, ci_content = ci_workflow
        ci_check = _check_yaml(ci_path, ci_content)
        if not ci_check.passed:
            logger.warning(
                "Generated CI workflow %s has invalid YAML syntax: %s. Using fallback workflow.",
                ci_path,
                ci_check.errors,
            )
            ci_workflow = generate_fallback_ci(repo_id, stack=stack)

    return test_files, ci_workflow


def _revise_tests(
    llm: LLMClient,
    context: TicketContext,
    current_tests: dict[str, str],
    reviewer_comments: list[str],
    ci_failure_output: str = "",
    gitlab: Any = None,
    tests_branch: str = "",
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """
    N2 fix: Uses ``chat_once()`` (stateless) so each revision cycle is a clean
    single-prompt call.  Prevents the 11K→78K token explosion seen in Issue #10
    where 5 revision cycles accumulated ~18K tokens per cycle.

    N5 fix: Includes CI failure output in the prompt so the LLM can debug
    test failures, not just respond to reviewer text.

    Also enforces N3 (unconditional assertions), N4 (CSS value extraction from
    actual source), N6 (absolute file paths), and N8 (clear variable naming).

    Fix 3 (ContextIssue): When ``gitlab`` and ``tests_branch`` are supplied,
    re-fetches each test file from the branch HEAD before building the prompt.
    This ensures the LLM operates on the latest committed tests rather than a
    potentially stale in-memory ``approved_tests`` dict.
    """
    ticket = context.ticket
    ticket_hint = f"{ticket.summary} {ticket.description} {ticket.acceptance_criteria}"
    stack = _detect_project_stack(context, ticket_hint=ticket_hint)

    # ── Fix 3 (ContextIssue): Re-fetch test files from tests_branch HEAD ─
    # The in-memory current_tests dict may lag behind what is actually
    # committed on tests_branch (e.g. after force-push or multi-cycle
    # revision). Refresh each path to guarantee the LLM sees reality.
    if gitlab and tests_branch:
        refreshed: dict[str, str] = {}
        for path, fallback_content in current_tests.items():
            try:
                fresh = gitlab.get_file_content(path, ref=tests_branch)
                refreshed[path] = fresh
            except Exception:
                # File may not exist on branch yet; keep in-memory content.
                refreshed[path] = fallback_content
        current_tests = refreshed
        logger.info(
            "Fix 3: Re-fetched %d test file(s) from branch '%s' for revision prompt.",
            len(current_tests),
            tests_branch,
        )

    comments_text = "\n".join(f"- {c}" for c in reviewer_comments)
    current_tests_text = "\n".join(
        f"### `{path}`\n```\n{content}\n```"
        for path, content in current_tests.items()
    )
    # Include the top source files so the LLM knows which paths/selectors exist.
    # S1-5 FIX: Prioritise LLM-selected files over symbol-padded fallbacks.
    # Fix 3: Also re-fetch source files from tests_branch when available, since
    # they may have been updated on that branch (e.g. fixture helpers added).
    source_summary = ""
    if context.source_files:
        priority_paths = context.llm_selected_paths or list(context.source_files.keys())
        source_parts: list[str] = []
        for path in priority_paths[:10]:
            if path not in context.source_files:
                continue
            content = context.source_files[path]
            if gitlab and tests_branch:
                try:
                    content = gitlab.get_file_content(path, ref=tests_branch)
                except Exception:
                    pass  # fall back to cached context content
            source_parts.append(f"### `{path}`\n```\n{content}\n```")
        source_summary = (
            "## Repository Source Files (for reference — read these to extract real CSS values)\n"
            + "\n".join(source_parts)
        )

    # N5: CI failure section
    ci_section = ""
    if ci_failure_output and ci_failure_output.strip():
        # Limit to last 60 lines to keep token cost bounded
        failure_lines = ci_failure_output.strip().splitlines()
        snippet = "\n".join(failure_lines[-60:])
        ci_section = (
            "## CI Test Failure Output (from last run)\n"
            "The following is the actual CI failure output from running the tests above. "
            "Use this to understand exactly which assertions failed and why:\n"
            f"```\n{snippet}\n```\n\n"
        )

    prompt = (
        "A human reviewer has requested changes to the test cases you generated earlier.\n"
        "You must revise the tests to address ALL reviewer feedback while staying true "
        "to the original ticket acceptance criteria.\n\n"
        "## Ticket\n"
        f"**ID:** {ticket.ticket_id}\n"
        f"**Summary:** {ticket.summary}\n"
        f"**Acceptance Criteria:**\n{ticket.acceptance_criteria}\n\n"
        f"## Reviewer Feedback\n{comments_text}\n\n"
        f"{ci_section}"
        f"## Current Test Files\n{current_tests_text}\n\n"
        f"{source_summary}\n\n"
        "## Stack-Specific Constraints\n"
        f"This is a **{stack}** project. Use only file paths, selectors, function names, "
        "and import paths that actually exist in the source files shown above.\n\n"
        "## Mandatory Test Quality Rules (N3/N4/N6/N8 — STRICTLY ENFORCED)\n"
        "1. **Unconditional assertions required:** Every test function MUST include at least one "
        "unconditional `assert` statement. Tests that ONLY assert inside `if 'prop' in style:` guards "
        "are NOT acceptable — they will silently pass on unchanged code. Rewrite conditional checks as:\n"
        "   ```python\n"
        "   # WRONG — silently passes if property is missing:\n"
        "   if 'background-color' in style:\n"
        "       assert style['background-color'] == '#1a1a1a'\n"
        "   # CORRECT — fails loudly if property is missing:\n"
        "   assert 'background-color' in style, 'background-color must be set'\n"
        "   assert style['background-color'] == '#1a1a1a'\n"
        "   ```\n"
        "2. **Extract CSS values from actual source files:** Do NOT hardcode expected CSS values from "
        "memory. Read the actual source CSS files shown above and use the exact property values that "
        "appear in those files. For CSS shorthand properties like `border-bottom: 1px solid #f2f2f2`, "
        "use the shorthand value string, not the longhand `border-bottom-color`.\n"
        "3. **Absolute file paths for file reads:** Use `os.path.join(os.path.dirname(__file__), "
        "'..', 'style.css')` or `os.path.abspath(...)` instead of `open('style.css')`. Relative paths "
        "break when pytest is run from a directory other than the repo root.\n"
        "4. **Clear, distinct variable names:** Use `original_sheets` for baseline snapshots and "
        "`current_sheets` for post-change state. Never use the same variable for both roles.\n"
        "5. Address ALL points raised in the reviewer's feedback.\n"
        "6. If the reviewer requested removing any tests or test files, DO NOT include them.\n"
        "7. Return ONLY the complete, final set of valid test files.\n"
        "8. Do NOT generate unnecessary tests for package-lock.json or build tools.\n"
        "9. Output format: fenced code blocks with `// FILE: path` comments.\n"
        "10. Do not include any explanation outside of code blocks."
    )

    # N2 fix: use chat_once() to prevent cross-cycle token accumulation.
    # Each revision cycle gets a clean context = system prompt + this single prompt.
    response = llm.chat_once(
        prompt,
        stage=2,
        call_purpose="Revise tests per Gate 1 reviewer feedback (stateless)",
        attached_files=list(current_tests.keys()),
    )
    content = response.get("content", "")
    revised = _parse_test_files(content)

    if not revised:
        logger.warning("LLM returned no parsed test files on revision; keeping existing tests.")
        return current_tests, None

    test_files: dict[str, str] = {}
    ci_workflow: tuple[str, str] | None = None

    for path, file_content in revised.items():
        norm = path.replace("\\", "/")
        if norm.startswith(".github/workflows/") or norm == ".gitlab-ci.yml":
            ci_workflow = (norm, file_content)
        else:
            test_files[norm] = file_content

    return test_files if test_files else current_tests, ci_workflow


def _validate_test_imports(
    test_files: dict[str, str],
    context: TicketContext,
) -> list[str]:
    """
    Issue 5 fix: Scan generated test files for import paths and file references
    that don't exist in the repository, flagging them before locking.

    Returns a list of warning strings (empty = all clear).
    """
    import re as _re
    warnings: list[str] = []
    all_paths_lower = {p.lower() for p in context.all_file_paths}
    all_basenames = {p.rsplit("/", 1)[-1].lower() for p in context.all_file_paths}

    # Patterns that reference repository files
    _q = r"[^'\"]"
    import_patterns = [
        # Python: from some.module import X  /  import some.module
        _re.compile(r"^(?:from|import)\s+([\w.]+)", _re.MULTILINE),
        # JS/TS relative import paths - match both quote styles via two alternates
        _re.compile(r"(?:from|require)\s*'(\./[^']+|\.\.?/[^']+)'", _re.MULTILINE),
        # Python open() file path references
        _re.compile(r"open\('((?!http)[^']+\.(?:html|css|js|ts|py))'\)", _re.MULTILINE),
    ]

    for test_path, content in test_files.items():
        for pat in import_patterns:
            for m in pat.finditer(content):
                ref = m.group(1).strip()
                # Only check relative file references (starting with . or /)
                if ref.startswith(("./")):
                    basename = ref.rsplit("/", 1)[-1].lower()
                    # Strip extension aliases
                    for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html"):
                        basename = basename.replace(ext, "")
                    if basename and not any(basename in p for p in all_paths_lower):
                        warnings.append(
                            f"`{test_path}` references `{ref}` which was not found in the repository file tree."
                        )
                elif ref.endswith((".html", ".css")):
                    # Direct file path references (e.g. open("index.html"))
                    basename = ref.rsplit("/", 1)[-1].lower()
                    if basename not in all_basenames:
                        warnings.append(
                            f"`{test_path}` opens `{ref}` which was not found in the repository."
                        )

        # N3 check: detect test functions whose ONLY asserts are inside if-guards.
        # These silently pass on unchanged code, providing no real test gate.
        _check_vacuous_assertions(test_path, content, warnings)

    return warnings


def _check_vacuous_assertions(
    test_path: str,
    content: str,
    warnings: list[str],
) -> None:
    """
    N3 fix: Scan a Python test file for test functions that have no unconditional
    assert at the top-level function body — i.e., all asserts are inside `if` blocks.
    These tests silently pass on unchanged code (they skip the assertion if the
    CSS property is absent, which is exactly what happens before dark-mode is applied).

    Appends warning strings to `warnings` for each offending function.
    """
    import re as _re

    # Only analyse Python test files (simple heuristic)
    if not test_path.endswith(".py"):
        return

    # Find all def test_*(self) or def test_*() functions
    fn_starts = [m.start() for m in _re.finditer(r"^def test_", content, _re.MULTILINE)]
    if not fn_starts:
        return

    for i, start in enumerate(fn_starts):
        end = fn_starts[i + 1] if i + 1 < len(fn_starts) else len(content)
        fn_body = content[start:end]

        # Extract function name
        fn_name_match = _re.match(r"def (test_\w+)", fn_body)
        fn_name = fn_name_match.group(1) if fn_name_match else "unknown"

        lines_in_fn = fn_body.splitlines()

        has_any_assert = any("assert" in line for line in lines_in_fn)
        if not has_any_assert:
            continue  # No asserts at all — different problem, not flagged here

        # Check if there is at least one unconditional assert (not indented inside an if)
        # An unconditional assert at function level has 4-space indent (or 1 tab) relative
        # to the def keyword, and is NOT preceded by an if-line at the same indent.
        has_unconditional = False
        prev_is_if = False
        fn_indent = len(lines_in_fn[0]) - len(lines_in_fn[0].lstrip()) if lines_in_fn else 0
        body_indent = fn_indent + 4  # one level deeper than def

        for line in lines_in_fn[1:]:
            stripped_line = line.lstrip()
            if not stripped_line:
                continue
            curr_indent = len(line) - len(stripped_line)
            if curr_indent == body_indent:
                if stripped_line.startswith("if ") or stripped_line.startswith("if("):
                    prev_is_if = True
                elif stripped_line.startswith("assert ") or stripped_line.startswith("assert("):
                    if not prev_is_if:
                        has_unconditional = True
                        break
                    prev_is_if = False  # Reset; next assert at this level is unconditional
                else:
                    prev_is_if = False

        if not has_unconditional:
            warnings.append(
                f"`{test_path}` — `{fn_name}()` has no unconditional top-level assert. "
                "All assertions appear to be inside `if` guards, which means the test "
                "silently passes if the expected CSS properties are absent. "
                "Add an unconditional `assert 'property' in style` before each guarded check."
            )


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
