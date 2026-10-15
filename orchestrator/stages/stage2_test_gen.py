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
from typing import Any

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.ci_generator import (
    find_existing_ci_workflow,
    generate_fallback_ci,
)
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
    Execute Stage 2: generate tests & workflow → open PR → await approval → lock.

    Returns
    -------
    dict[str, str]
        ``{file_path: content}`` of the approved & locked test files.
    """
    audit.log(stage=2, event_type="stage2_start")
    ticket = context.ticket

    # ── 1. Create the tests-only branch ─────────────────────────────────
    logger.info("Creating test branch…")
    tests_branch = f"automation/{ticket.ticket_id}/tests"
    if not gitlab.branch_exists(tests_branch):
        gitlab.create_branch(tests_branch, ref=context.default_branch)
    checkpoint.tests_branch = tests_branch

    # Inspect for existing CI workflow on default branch / repository
    existing_ci_path, existing_ci_content = find_existing_ci_workflow(
        gitlab=gitlab,
        branch=context.default_branch,
        all_file_paths=context.all_file_paths,
    )

    # ── 2. Generate test cases & repo-specific CI via LLM ───────────────
    logger.info("Calling LLM to generate test cases and evaluate CI workflow…")
    repo_id = getattr(gitlab, "project_id", None) or getattr(gitlab, "repo_name", "target-repo")
    generated_tests, ci_workflow_update = _generate_tests(
        llm=llm,
        context=context,
        existing_ci_path=existing_ci_path,
        existing_ci_content=existing_ci_content,
        repo_id=str(repo_id),
    )
    logger.info("LLM generated %d test file(s)", len(generated_tests))

    if not generated_tests:
        logger.error("LLM returned no test files for ticket %s", ticket.ticket_id)
        raise RuntimeError(
            f"Stage 2 failed: LLM did not generate any test files matching the expected format for ticket {ticket.ticket_id}."
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
        # Fallback if LLM generated neither tests nor fallback workflow
        stack = _detect_project_stack(context)
        ci_workflow_path, ci_workflow_content = generate_fallback_ci(str(repo_id), stack=stack)
        ci_created_or_updated = True

    checkpoint.ci_workflow_path = ci_workflow_path

    # ── 3. Commit test files & CI workflow ──────────────────────────────
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
                ci_workflow_path=ci_workflow_path if ci_created_or_updated else None,
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

    # Pre-seed seen_comments with any comments already present on the PR
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
            # N5 fix: surface CI failure logs in the revision prompt so the LLM
            # knows exactly which tests failed and why (not just reviewer text).
            last_ci_output = getattr(gate_result, "ci_output", "") or ""
            revised_tests, revised_ci = _revise_tests(
                llm, context, approved_tests, gate_result.reviewer_comments,
                ci_failure_output=last_ci_output,
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
            revision_commit_files = dict(approved_tests)
            if revised_ci:
                revision_commit_files[revised_ci[0]] = revised_ci[1]

            gitlab.commit_files(
                branch=tests_branch,
                files=revision_commit_files,
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

    # ── 6. Pre-lock symbol validation (Issue 5 fix) ───────────────────
    # Verify that import paths and file references in the tests actually
    # exist in the repository before locking them permanently.
    validation_warnings = _validate_test_imports(approved_tests, context)
    if validation_warnings:
        logger.warning(
            "Pre-lock validation found %d potential issue(s) in test files:\n%s",
            len(validation_warnings),
            "\n".join(f"  - {w}" for w in validation_warnings),
        )
        # Log to audit but do NOT block — human reviewer already approved.
        # The warnings are surfaced so the reviewer can be informed.

    # ── 7. Lock test files ──────────────────────────────────────────────
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


def _detect_project_stack(context: TicketContext, ticket_hint: str = "") -> str:
    """
    Issue 7 fix: Detect which layer of the stack is RELEVANT TO THIS TICKET,
    not just what languages exist somewhere in the repo.

    Polyglot repos (Django+React, FastAPI+Vue) used to always return "python"
    because Python files were found first. Now we use the ticket text and the
    source_files already selected for this ticket to infer the relevant layer.
    """
    paths = [p.lower() for p in context.all_file_paths]
    # Combine ticket text + selected source file paths for relevance hint
    ticket_lower = ticket_hint.lower()
    selected = [p.lower() for p in context.source_files.keys()]

    # ── Tier 1: check what the SELECTED source files actually are ────────
    # This is the most reliable signal — Stage 1 already ranked the files
    # most relevant to the ticket. If those files are CSS/HTML, it's a UI ticket.
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

    # ── Tier 2: ticket text hints (e.g. "CSS dark mode" → static_web) ──
    frontend_keywords = ("css", "html", "style", "color", "layout", "ui ", "frontend", "dark mode", "responsive")
    react_keywords    = ("react", "component", "tsx", "jsx", "hook", "props", "state", "render")
    if any(k in ticket_lower for k in react_keywords):
        return "react"
    if any(k in ticket_lower for k in frontend_keywords):
        # Only use static_web if no package.json exists (otherwise it's a React/Node project)
        has_pkg_json = any(p == "package.json" or p.endswith("/package.json") for p in paths)
        return "node" if has_pkg_json else "static_web"

    # ── Tier 3: repo-wide fallback (original priority queue) ────────────
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


def _generate_tests(
    llm: LLMClient,
    context: TicketContext,
    existing_ci_path: str | None = None,
    existing_ci_content: str | None = None,
    repo_id: str = "target-repo",
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Prompt the LLM to generate test cases and appropriate CI workflow from the ticket spec."""
    ticket = context.ticket
    ticket_hint = f"{ticket.summary} {ticket.description} {ticket.acceptance_criteria}"
    stack = _detect_project_stack(context, ticket_hint=ticket_hint)

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
    if stack == "python":
        stack_instructions = (
            "### Technology Stack: Python\n"
            "- Use `pytest` for test cases.\n"
            "- Follow naming convention: `tests/test_<feature>.py`.\n"
            "- Directly test functions, classes, or endpoints specified in the acceptance criteria.\n"
            "- Assert specific expected return values, schemas, error exceptions, and side-effects.\n"
            "- STRICTLY FORBIDDEN: Writing trivial tests like `assert result is not None`, `assert True`, or mocking away the logic being tested.\n"
            "- DO NOT import JavaScript, Jest, React, or npm modules.\n"
        )
    elif stack == "react":
        stack_instructions = (
            "### Technology Stack: React\n"
            "- Use Jest / Vitest + React Testing Library.\n"
            "- Follow naming convention: `<module>.test.tsx` or `<module>.test.ts`.\n"
            "- Test component behavior, user events (clicks, typing), props, and rendered UI elements.\n"
            "- Assert real visible text, rendered data, and state updates matching acceptance criteria.\n"
            "- STRICTLY FORBIDDEN: Shallow snapshot tests or tests that merely verify an empty wrapper div rendered.\n"
            "- DO NOT generate irrelevant tests for package managers or environment setup.\n"
        )
    elif stack == "node":
        stack_instructions = (
            "### Technology Stack: Node.js / JavaScript\n"
            "- Use Jest / Vitest or the repo's existing test framework.\n"
            "- Follow naming convention: `<module>.test.ts` or `<module>.test.js`.\n"
            "- Directly test business logic, routes, and data transformations.\n"
            "- Assert exact payloads, HTTP status codes, and error conditions.\n"
            "- STRICTLY FORBIDDEN: Trivial tests that merely check if an endpoint exists without verifying its returned data.\n"
            "- DO NOT generate irrelevant tests for package managers or environment setup.\n"
        )
    elif stack == "java":
        stack_instructions = (
            "### Technology Stack: Java\n"
            "- Use JUnit 5 for test cases.\n"
            "- Follow standard Maven/Gradle naming: `src/test/java/.../*Test.java`.\n"
            "- Assert explicit business logic, return values, and contract requirements.\n"
            "- STRICTLY FORBIDDEN: Dummy assertions like `assertTrue(true)` or `assertNotNull(obj)` without value checks.\n"
        )
    elif stack == "go":
        stack_instructions = (
            "### Technology Stack: Go\n"
            "- Use standard Go `testing` package.\n"
            "- Follow naming convention: `<package>_test.go`.\n"
            "- Assert concrete outputs, error types, and boundary conditions.\n"
        )
    elif stack == "rust":
        stack_instructions = (
            "### Technology Stack: Rust\n"
            "- Use standard Rust test modules (`#[cfg(test)]`) or `tests/<feature>_test.rs`.\n"
            "- Assert explicit state mutations, return values, and error variants with strict `assert_eq!`.\n"
        )
    elif stack == "static_web":
        stack_instructions = (
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
    else:
        stack_instructions = (
            "### Technology Stack: Multi-Language / Generic\n"
            "- Inspect the existing repository files and test frameworks.\n"
            "- Write tests matching the repository's native conventions.\n"
            "- Assert real functional outputs and data transformations; never write dummy assertions.\n"
        )

    # CI Workflow instructions
    if existing_ci_path and existing_ci_content:
        ci_instructions = (
            f"## Existing CI/CD Workflow (`{existing_ci_path}`)\n"
            f"```yaml\n{existing_ci_content}\n```\n\n"
            f"Evaluate if this workflow needs updates to run the new test cases (e.g. adding test execution steps, env variables, or dependencies).\n"
            f"- If updates ARE needed: Return the complete, updated workflow file: `// FILE: {existing_ci_path}`.\n"
            f"- If NO updates are needed: Do NOT output `{existing_ci_path}`. Keep the existing workflow as is.\n"
        )
    else:
        ci_instructions = (
            "## CI/CD Workflow Generation Required\n"
            "No existing CI/CD workflow was found in `.github/workflows/`.\n"
            "You MUST generate a complete, working GitHub Actions workflow file tailored specifically to this repository (e.g. `// FILE: .github/workflows/ci.yml`).\n"
            "Requirements for the workflow:\n"
            "1. `runs-on: ubuntu-latest`\n"
            "2. Triggers:\n"
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
            "3. Concurrency control:\n"
            "   ```yaml\n"
            "   concurrency:\n"
            "     group: ${{ github.workflow }}-${{ github.ref }}\n"
            "     cancel-in-progress: true\n"
            "   ```\n"
            "4. Steps: checkout (`actions/checkout@v4`), setup appropriate runtime matching the repository's stack, install dependencies, and run tests.\n"
        )

    prompt = (
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
        f"{ci_instructions}\n"
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

    response = llm.chat(
        prompt,
        stage=2,
        call_purpose="Generate pre-development tests and CI workflow",
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

    return test_files, ci_workflow


def _revise_tests(
    llm: LLMClient,
    context: TicketContext,
    current_tests: dict[str, str],
    reviewer_comments: list[str],
    ci_failure_output: str = "",
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """
    N2 fix: Uses ``chat_once()`` (stateless) so each revision cycle is a clean
    single-prompt call.  Prevents the 11K→78K token explosion seen in Issue #10
    where 5 revision cycles accumulated ~18K tokens per cycle.

    N5 fix: Includes CI failure output in the prompt so the LLM can debug
    test failures, not just respond to reviewer text.

    Also enforces N3 (unconditional assertions), N4 (CSS value extraction from
    actual source), N6 (absolute file paths), and N8 (clear variable naming).
    """
    ticket = context.ticket
    ticket_hint = f"{ticket.summary} {ticket.description} {ticket.acceptance_criteria}"
    stack = _detect_project_stack(context, ticket_hint=ticket_hint)

    comments_text = "\n".join(f"- {c}" for c in reviewer_comments)
    current_tests_text = "\n".join(
        f"### `{path}`\n```\n{content}\n```"
        for path, content in current_tests.items()
    )
    # Include the top 10 source files so the LLM knows which paths/selectors exist
    source_summary = ""
    if context.source_files:
        source_summary = (
            "## Repository Source Files (for reference — read these to extract real CSS values)\n"
            + "\n".join(
                f"### `{path}`\n```\n{content}\n```"
                for path, content in list(context.source_files.items())[:10]
            )
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
