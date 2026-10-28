"""
Orchestrator entry point — CLI & Webhook Server (Phase 1–4).

Supports two execution modes:
  1. CLI Mode: `python -m orchestrator.main PROJ-123`
  2. Server Mode: `python -m orchestrator.main --server`

Runs the end-to-end spec-driven pipeline:
  Stage 1: Context assembly & qualification triage
  Stage 2: Test generation + Gate 1 approval
  Stage 3: Iterative code generation + tamper-checking
  Stage 4: Full CI/CD validation gates (ESLint, Semgrep, Trivy, Jest, SonarQube)
  Stage 5: PR promotion + Gate 2 approval + reviewer feedback routing (FR-21)
"""

from __future__ import annotations

import argparse
import logging
import re as _re
import time as _time
from typing import Any

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.config import Settings, get_settings
from orchestrator.exceptions import GatePendingError
from orchestrator.github_client import GitHubClient
from orchestrator.github_issue_client import GitHubIssueClient
from orchestrator.llm_client import LLMClient
from orchestrator.rate_limiter import RateLimiter
from orchestrator.repo_resolver import RepoResolver
from orchestrator.stages import (
    stage1_context,
    stage2_test_gen,
    stage3_code_gen,
    stage4_validation,
    stage5_pr,
)
from orchestrator.stages.stage1_context import refresh_context_for_feedback

logger = logging.getLogger("orchestrator")

_SYSTEM_PROMPT = (
    "You are a spec-driven development agent.  Your job is to analyse "
    "tickets, generate test cases from acceptance criteria, and implement "
    "code changes that make those tests pass.  Always follow the file-path "
    "output format specified in each prompt."
)


def _setup_logging(verbose: bool = False, debug_wire: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(name)-28s  %(levelname)-5s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Always silence noisy 3rd-party HTTP transport and client wire dumps unless debug_wire is explicitly enabled
    if not debug_wire:
        noisy_wire_loggers = (
            "urllib3",
            "urllib3.connectionpool",
            "requests",
            "httpx",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "openai",
            "openai._base_client",
            "github",
            "github.Requester",
            "git",
        )
        for noisy in noisy_wire_loggers:
            logging.getLogger(noisy).setLevel(logging.WARNING)


def run_pipeline(
    ticket_id: str,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> int:
    """
    Execute the full end-to-end Spec-Driven Development pipeline for *ticket_id*.

    Returns 0 on success (MR promoted & approved), 1 on failure/escalation.
    """
    logger.info("═" * 60)
    logger.info("  Spec-Driven Pipeline — Processing %s", ticket_id)
    logger.info("═" * 60)

    if settings is None:
        settings = get_settings()

    rate_limiter = RateLimiter(max_calls=settings.rate_limit_cap)

    issue_client = GitHubIssueClient(
        token=settings.github_token,
        repo=settings.github_repo,
        base_url=settings.github_base_url,
    )
    vcs_client = GitHubClient(
        token=settings.github_token,
        repo=settings.github_repo,
        base_url=settings.github_base_url,
        workflow_file=settings.github_workflow_file or None,
    )
    jira = issue_client
    gitlab = vcs_client

    llm = LLMClient(
        api_key=settings.nim_api_key,
        base_url=settings.nim_base_url,
        model=settings.nim_model,
        fallback_model=settings.nim_fallback_model,
        rate_limiter=rate_limiter,
        system_prompt=_SYSTEM_PROMPT,
        enable_context_pruning=settings.enable_context_pruning,
        max_context_tokens=settings.max_context_tokens,
    )

    repo_resolver = RepoResolver(settings.repo_mapping)

    audit = AuditLogger(ticket_id=ticket_id, audit_dir=settings.audit_dir)
    llm.set_audit_logger(audit)

    # ── Checkpoint: resume or create ────────────────────────────────────
    checkpoint = Checkpoint.load(ticket_id, settings.checkpoint_dir)
    if checkpoint:
        checkpoint.checkpoint_dir = settings.checkpoint_dir
        if checkpoint.repo_id and str(checkpoint.repo_id) != str(gitlab.project_id):
            mapped_repos = set(str(v) for v in repo_resolver.mapping.values()) if repo_resolver else set()
            if str(checkpoint.repo_id) in mapped_repos:
                logger.info("Resuming ticket in mapped repository ID %s", checkpoint.repo_id)
                gitlab.switch_project(checkpoint.repo_id)
            else:
                logger.warning(
                    "Checkpoint %s belongs to repo '%s', but current active repo is '%s'. Ignoring stale checkpoint and starting fresh.",
                    ticket_id,
                    checkpoint.repo_id,
                    gitlab.project_id,
                )
                checkpoint = Checkpoint(ticket_id=ticket_id, checkpoint_dir=settings.checkpoint_dir)
                checkpoint.repo_id = gitlab.project_id

        if checkpoint.gate_waiting == "gate1":
            checkpoint.current_stage = 2
            logger.info(
                "Resuming at Gate 1 checkpoint (PR #%d)",
                checkpoint.gate_waiting_mr_iid,
            )
        elif checkpoint.gate_waiting == "gate2":
            checkpoint.current_stage = 5
            logger.info(
                "Resuming at Gate 2 checkpoint (PR #%d)",
                checkpoint.gate_waiting_mr_iid,
            )

        if checkpoint.gate_waiting and checkpoint.gate_waiting_since:
            elapsed_hours = (_time.time() - checkpoint.gate_waiting_since) / 3600
            if elapsed_hours > settings.gate_timeout_hours:
                logger.error(
                    "Gate '%s' for ticket %s timed out after %.1fh.",
                    checkpoint.gate_waiting,
                    ticket_id,
                    elapsed_hours,
                )
                issue_client.post_lifecycle_comment(
                    ticket_id=ticket_id,
                    title="Gate Timeout",
                    message=(
                        f"Gate '{checkpoint.gate_waiting}' timed out after "
                        f"{settings.gate_timeout_hours}h with no human review. "
                        "Pipeline halted. Please review manually."
                    ),
                )
                return 1

        if checkpoint.current_stage > 1:
            logger.info(
                "Resuming from checkpoint (stage=%d, iter=%d)",
                checkpoint.current_stage,
                checkpoint.iteration_count,
            )
            if checkpoint.conversation_history:
                llm.set_history(checkpoint.conversation_history)
        else:
            logger.info("Starting fresh from Stage 1.")
    else:
        checkpoint = Checkpoint(ticket_id=ticket_id, checkpoint_dir=settings.checkpoint_dir)
        checkpoint.repo_id = gitlab.project_id
        logger.info("No checkpoint found — starting fresh.")

    audit.log(
        stage=0,
        event_type="pipeline_start",
        payload={"resumed": checkpoint.current_stage > 1},
    )

    if dry_run:
        logger.info("Dry-run mode — configuration validated. Exiting.")
        return 0

    try:
        # Notify JIRA of pipeline start if starting fresh
        if checkpoint.current_stage <= 1:
            jira.transition_ticket(ticket_id, ["In Progress", "Start Progress"])
            jira.post_lifecycle_comment(
                ticket_id=ticket_id,
                title="Pipeline Started",
                message="Spec-Driven Automation pipeline has started processing this ticket.",
            )

        # ── Stage 1: Assemble Context ───────────────────────────────────
        if checkpoint.current_stage <= 1:
            logger.info("▶ Stage 1: Assemble Ticket Context")
            context = stage1_context.run(
                ticket_id=ticket_id,
                jira=jira,
                gitlab=gitlab,
                llm=llm,
                checkpoint=checkpoint,
                audit=audit,
                qualifying_label=settings.qualifying_label,
                repo_resolver=repo_resolver,
                max_context_test_files=settings.max_context_test_files,
            )
            checkpoint.conversation_history = llm.history
            checkpoint.save(settings.checkpoint_dir)
        else:
            logger.info(
                "▶ Stage 1: Re-fetching context (resuming from Stage %d)",
                checkpoint.current_stage,
            )
            context = stage1_context.run(
                ticket_id=ticket_id,
                jira=jira,
                gitlab=gitlab,
                llm=llm,
                checkpoint=checkpoint,
                audit=audit,
                qualifying_label=settings.qualifying_label,
                repo_resolver=repo_resolver,
                max_context_test_files=settings.max_context_test_files,
            )

        # ── Stage 2: Generate Tests + Gate 1 ────────────────────────────
        if checkpoint.current_stage <= 2 and not checkpoint.gate1_approved:
            logger.info("▶ Stage 2: Generate Test Cases + Human Gate 1")
            try:
                locked_tests = stage2_test_gen.run(
                    context=context,
                    gitlab=gitlab,
                    llm=llm,
                    checkpoint=checkpoint,
                    audit=audit,
                    gate_poll_interval=settings.gate_poll_interval_seconds,
                    gate_timeout_hours=settings.gate_timeout_hours,
                    max_revision_cycles=settings.max_gate1_revision_cycles,
                    test_preflight_max_repairs=settings.test_preflight_max_repairs,
                    blocking_gate=settings.blocking_gates,
                    checkpoint_dir=settings.checkpoint_dir,
                )
                checkpoint.conversation_history = llm.history
                checkpoint.save(settings.checkpoint_dir)
                logger.info(
                    "Stage 2 complete — %d test file(s) locked.",
                    len(locked_tests),
                )
            except GatePendingError as e:
                checkpoint.conversation_history = llm.history
                checkpoint.save(settings.checkpoint_dir)
                logger.info(
                    "Pipeline suspended at %s for ticket %s (PR/MR #%d). "
                    "Ticket is parked until review event occurs.",
                    e.gate_name,
                    ticket_id,
                    e.mr_iid,
                )
                return 0

        # ── Stages 3, 4, 5 Loop with Reviewer Feedback (FR-21) ──────────
        while True:
            # ── Stage 3: Iterative Code Generation ──────────────────────
            if checkpoint.current_stage <= 3:
                logger.info("▶ Stage 3: Iterative Code Generation")
                stage3_code_gen.run(
                    context=context,
                    gitlab=gitlab,
                    llm=llm,
                    jira=jira,
                    checkpoint=checkpoint,
                    audit=audit,
                    max_iterations=settings.max_code_iterations,
                    ci_poll_interval=settings.ci_poll_interval_seconds,
                    ci_timeout_minutes=settings.ci_timeout_minutes,
                    slack_webhook_url=settings.slack_webhook_url,
                    escalation_email=settings.escalation_email,
                    reviewer_feedback=checkpoint.reviewer_feedback,
                    reuse_stage3_final_pipeline=settings.reuse_stage3_final_pipeline,
                )
                checkpoint.conversation_history = llm.history
                checkpoint.save(settings.checkpoint_dir)
                logger.info("Stage 3 complete — all target tests pass.")

            # ── Stage 4: CI/CD Validation ───────────────────────────────
            if checkpoint.current_stage <= 4:
                logger.info("▶ Stage 4: Full CI/CD Validation")
                validation_result = stage4_validation.run(
                    code_branch=checkpoint.code_branch,
                    gitlab=gitlab,
                    checkpoint=checkpoint,
                    audit=audit,
                    ci_poll_interval=settings.ci_poll_interval_seconds,
                    ci_timeout_minutes=settings.ci_timeout_minutes,
                    sonar_configured=bool(settings.sonar_host_url),
                    coverage_threshold=settings.coverage_threshold,
                    reuse_stage3_pipeline=settings.reuse_stage3_final_pipeline,
                )
                checkpoint.save(settings.checkpoint_dir)

                # S4-1: Repair loop for Stage 4 gate failures before escalating
                if not validation_result.passed:
                    failed_gates = [g for g in validation_result.gates if g.required and not g.passed]
                    logger.warning(
                        "Stage 4 validation FAILED on required gates: %s",
                        [g.name for g in failed_gates],
                    )

                    repair_attempts = 0
                    max_repairs = getattr(settings, "max_stage4_repair_attempts", 1)

                    while not validation_result.passed and repair_attempts < max_repairs:
                        repair_attempts += 1
                        logger.info(
                            "Attempting Stage 4 LLM repair (attempt %d/%d) for gates: %s",
                            repair_attempts, max_repairs, [g.name for g in failed_gates],
                        )
                        gate_logs: dict[str, str] = {}
                        for gate in failed_gates:
                            log = ""
                            if hasattr(gitlab, "get_job_log"):
                                log = gitlab.get_job_log(validation_result.pipeline_id, gate.name)
                            if log:
                                gate_logs[gate.name] = log

                        if not gate_logs:
                            gate_logs = {g.name: f"Quality gate '{g.name}' failed with status '{g.status}'." for g in failed_gates}

                        repaired = _attempt_stage4_repair(
                            context=context,
                            checkpoint=checkpoint,
                            gitlab=gitlab,
                            llm=llm,
                            audit=audit,
                            gate_logs=gate_logs,
                        )
                        if not repaired:
                            logger.error("Stage 4 repair produced no valid changes.")
                            break

                        # Re-run Stage 4 validation with a fresh CI pipeline (not reused)
                        validation_result = stage4_validation.run(
                            code_branch=checkpoint.code_branch,
                            gitlab=gitlab,
                            checkpoint=checkpoint,
                            audit=audit,
                            ci_poll_interval=settings.ci_poll_interval_seconds,
                            ci_timeout_minutes=settings.ci_timeout_minutes,
                            sonar_configured=bool(settings.sonar_host_url),
                            coverage_threshold=settings.coverage_threshold,
                            reuse_stage3_pipeline=False,
                        )
                        checkpoint.save(settings.checkpoint_dir)
                        failed_gates = [g for g in validation_result.gates if g.required and not g.passed]

                if not validation_result.passed:
                    logger.error(
                        "Stage 4 validation FAILED. Pipeline halted. See %s",
                        validation_result.pipeline_url,
                    )
                    return 1
                logger.info("Stage 4 complete — all validation gates passed.")
            else:
                # Reconstruct validation result if resuming past Stage 4
                validation_result = stage4_validation.ValidationResult(
                    pipeline_id=checkpoint.ci_pipeline_id or 0,
                    pipeline_url=checkpoint.validation_result.get("pipeline_url", ""),
                    overall_status="passed",
                )

            # ── Stage 5: PR Promotion & Gate 2 ──────────────────────────
            logger.info("▶ Stage 5: PR Promotion & Human Gate 2")
            try:
                stage5_result = stage5_pr.run(
                    ticket_id=ticket_id,
                    context=context,
                    validation_result=validation_result,
                    checkpoint=checkpoint,
                    audit=audit,
                    gitlab=gitlab,
                    jira=jira,
                    gate_poll_interval=settings.gate_poll_interval_seconds,
                    gate_timeout_hours=settings.gate_timeout_hours,
                    slack_webhook_url=settings.slack_webhook_url,
                    escalation_email=settings.escalation_email,
                    blocking_gate=settings.blocking_gates,
                    checkpoint_dir=settings.checkpoint_dir,
                )
            except GatePendingError as e:
                checkpoint.conversation_history = llm.history
                checkpoint.save(settings.checkpoint_dir)
                logger.info(
                    "Pipeline suspended at %s for ticket %s (PR/MR #%d). "
                    "Ticket is parked until review event occurs.",
                    e.gate_name,
                    ticket_id,
                    e.mr_iid,
                )
                audit.log(
                    stage=5,
                    event_type="gate2_pending",
                    payload={"mr_iid": e.mr_iid},
                )
                return 0

            checkpoint.save(settings.checkpoint_dir)

            if stage5_result.status == "timeout":
                logger.error("Gate 2 timed out for PR #%d on ticket %s.", stage5_result.mr_iid, ticket_id)
                jira.post_lifecycle_comment(
                    ticket_id=ticket_id,
                    title="Gate 2 Approval Timeout",
                    message=(
                        f"Gate 2 approval timed out after {settings.gate_timeout_hours} hours on PR #{stage5_result.mr_iid}. "
                        "Pipeline suspended. Human intervention required."
                    ),
                    mr_url=stage5_result.mr_url,
                )
                return 1

            if stage5_result.approved:
                logger.info("Stage 5 complete — MR !%d approved by reviewer.", stage5_result.mr_iid)
                break

            if stage5_result.changes_requested:
                if checkpoint.review_iteration_count >= settings.max_review_iterations:
                    logger.warning(
                        "Max review iterations (%d) reached for %s. Escalating to human.",
                        settings.max_review_iterations,
                        ticket_id,
                    )
                    jira.post_lifecycle_comment(
                        ticket_id=ticket_id,
                        title="Review Iterations Exhausted",
                        message=(
                            f"The automated pipeline reached the limit of "
                            f"{settings.max_review_iterations} review iteration(s). "
                            f"Please take over this ticket manually."
                        ),
                        mr_url=stage5_result.mr_url,
                    )
                    return 1

                checkpoint.review_iteration_count += 1
                checkpoint.reviewer_feedback = stage5_result.reviewer_feedback
                checkpoint.reviewer_feedback_history.append(list(stage5_result.reviewer_feedback))
                logger.info(
                    "Reviewer requested changes (cycle %d/%d). Re-routing to Stage 3.",
                    checkpoint.review_iteration_count,
                    settings.max_review_iterations,
                )

                # Phase 2: Expand context with files referenced by reviewer.
                # Fix 2 (ContextIssue): pass checkpoint so the expansion
                # fetches from code_branch instead of default_branch.
                logger.info("Phase 2: Expanding context for reviewer feedback…")
                refresh_context_for_feedback(
                    reviewer_feedback=stage5_result.reviewer_feedback,
                    context=context,
                    gitlab=gitlab,
                    audit=audit,
                    checkpoint=checkpoint,
                )

                # GAP-5: Fetch PR diff so Stage 3 knows what was already done.
                # Injecting the diff into the coding prompt prevents the LLM
                # from repeating or reverting already-committed changes.
                if checkpoint.code_branch and hasattr(gitlab, "get_branch_diff"):
                    try:
                        pr_diff = gitlab.get_branch_diff(
                            source_branch=checkpoint.code_branch,
                            target_branch=context.default_branch,
                            max_lines=400,
                        )
                        if pr_diff:
                            checkpoint.pr_diff_summary = pr_diff
                            logger.info(
                                "PR diff fetched: %d chars from %s → %s.",
                                len(pr_diff),
                                context.default_branch,
                                checkpoint.code_branch,
                            )
                            audit.log(
                                stage=3,
                                event_type="pr_diff_fetched",
                                payload={
                                    "code_branch": checkpoint.code_branch,
                                    "diff_chars": len(pr_diff),
                                },
                            )
                    except Exception as _diff_exc:
                        logger.warning("Could not fetch PR diff: %s", _diff_exc)

                # Issue 12 fix: DO NOT call llm.append_user_message(feedback_prompt) here.
                # Stage 3's _generate_code() already injects reviewer_feedback via its
                # own feedback_section in the prompt. Injecting here too causes the
                # feedback to appear twice per iteration (4x after cycle 2, 6x after
                # cycle 3...) and gets truncated by context_pruner to unreadable snippets.
                # The reviewer feedback is stored in checkpoint.reviewer_feedback and
                # passed explicitly to stage3_code_gen.run() via the reviewer_feedback arg.
                checkpoint.conversation_history = llm.history
                checkpoint.current_stage = 3
                checkpoint.iteration_count = 0
                checkpoint.save(settings.checkpoint_dir)
                continue


            logger.error("Stage 5 ended without approval (status=%s)", stage5_result.status)
            return 1

        # ── Pipeline Complete ───────────────────────────────────────────
        audit.log(stage=0, event_type="pipeline_complete")
        logger.info("Pipeline finished successfully for %s.", ticket_id)

        # ── Phase 4: Audit Log Governance & Shipping ────────────────────
        from orchestrator.audit_shipper import ship_audit_log
        ship_result = ship_audit_log(
            ticket_id=ticket_id,
            audit_dir=settings.audit_dir,
            settings=settings,
            gitlab=gitlab,
        )
        logger.info("Audit log shipping result: %s", ship_result.get("status"))

        return 0

    except Exception:
        logger.exception("Pipeline failed for %s", ticket_id)
        audit.log(stage=checkpoint.current_stage, event_type="pipeline_error")
        checkpoint.conversation_history = llm.history
        checkpoint.save(settings.checkpoint_dir)
        return 1


def _attempt_stage4_repair(
    context: stage1_context.TicketContext,
    checkpoint: Checkpoint,
    gitlab: Any,
    llm: LLMClient,
    audit: AuditLogger,
    gate_logs: dict[str, str],
) -> bool:
    """
    Prompt the LLM to fix Stage 4 validation failures (e.g. lint, SAST, security findings).
    Pre-flights the patched files, verifies tamper protection, and commits to code_branch.
    Returns True if valid repaired files were committed.
    """
    from orchestrator.diff_patcher import apply_patches_to_sources, parse_llm_response
    from orchestrator.preflight_checker import check_all
    from orchestrator.stages.stage3_code_gen import _lang_for_path

    gate_sections = []
    for gate_name, log in gate_logs.items():
        log_lines = log.strip().splitlines()
        trimmed_log = "\n".join(log_lines[-150:]) if len(log_lines) > 150 else log.strip()
        gate_sections.append(f"## Gate Failure: {gate_name.upper()}\n```\n{trimmed_log}\n```")

    gate_failure_section = "\n\n".join(gate_sections)

    # Refresh current sources from code_branch
    current_sources: dict[str, str] = dict(context.source_files)
    for path in list(current_sources.keys()):
        try:
            content = gitlab.get_file_content(path, ref=checkpoint.code_branch)
            if content is not None:
                current_sources[path] = content
        except Exception:
            pass

    # Exclude locked test files and CI workflows from editable sources
    locked_paths = set(checkpoint.locked_test_hashes.keys())
    blocked_paths = set(locked_paths)
    if checkpoint.ci_workflow_path:
        blocked_paths.add(checkpoint.ci_workflow_path)

    editable_sources = {k: v for k, v in current_sources.items() if k not in blocked_paths}

    mentioned_in_logs: set[str] = set()
    for log_text in gate_logs.values():
        for match in _re.finditer(
            r'[\w./\-]+\.(?:js|ts|tsx|jsx|py|css|html|json)\b', log_text
        ):
            mentioned_in_logs.add(match.group(0).lstrip("./"))

    prioritized = [p for p in editable_sources if any(m in p for m in mentioned_in_logs)]
    rest = [p for p in editable_sources if p not in set(prioritized)]
    ordered_sources = {p: editable_sources[p] for p in (prioritized + rest)[:10]}

    source_parts = []
    for path, content in ordered_sources.items():
        source_parts.append(f"### `{path}`\n```{_lang_for_path(path)}\n{content}\n```")
    source_summary = "\n\n".join(source_parts)

    prompt = (
        "You are a senior software engineer acting as an automated repair agent. "
        "The automated code generation passed initial target tests, but subsequent "
        "CI/CD quality gates (such as lint, SAST, or dependency checks) failed.\n\n"
        "Your task is to fix the issues described in the gate logs below.\n\n"
        f"{gate_failure_section}\n\n"
        f"## Current Source Files\n{source_summary}\n\n"
        "## Critical Rules\n"
        "1. Fix ONLY the lint errors, security warnings, or code quality issues reported above.\n"
        "2. Do NOT change test logic, acceptance criteria, or unrelated features.\n"
        "3. IMMUTABLE FILES: Do NOT modify any locked test files or CI workflow files.\n"
        "4. Respond ONLY using SEARCH/REPLACE blocks for modified files:\n"
        "```\n"
        "// FILE: path/to/file.ext\n"
        "<<<<<<< SEARCH\n"
        "[exact original lines]\n"
        "=======\n"
        "[replacement lines]\n"
        ">>>>>>> REPLACE\n"
        "```"
    )

    llm.clear_history(keep_system=True)
    response = llm.chat(
        prompt,
        stage=4,
        call_purpose="Stage 4 validation gate failure repair",
        attached_files=list(ordered_sources.keys()),
    )
    raw_content = response.get("content", "")
    parsed = parse_llm_response(raw_content)
    if not parsed:
        logger.warning("Stage 4 repair: LLM returned no SEARCH/REPLACE blocks.")
        return False

    fixed_files, patch_results, all_succeeded = apply_patches_to_sources(parsed, current_sources)
    if not all_succeeded or not fixed_files:
        logger.warning("Stage 4 repair: Patches failed to apply cleanly.")
        return False

    # Check for immutable file violations
    violations = set(fixed_files.keys()) & blocked_paths
    if violations:
        logger.warning("Stage 4 repair: LLM attempted to modify locked files: %s", violations)
        return False

    # Pre-flight syntax verification
    passed_preflight, preflight_results = check_all(fixed_files)
    if not passed_preflight:
        logger.warning(
            "Stage 4 repair: Repaired files failed pre-flight syntax check: %s",
            [r.errors for r in preflight_results if not r.passed],
        )
        return False

    try:
        gitlab.commit_files(
            branch=checkpoint.code_branch,
            files=fixed_files,
            commit_message="[spec-driven] Stage 4 auto-repair: fix CI quality gate failures",
            blocked_paths=blocked_paths,
        )
        checkpoint.modified_files = list(set(checkpoint.modified_files) | set(fixed_files.keys()))
        audit.log(
            stage=4,
            event_type="stage4_repair_committed",
            payload={"fixed_files": list(fixed_files.keys())},
        )
        logger.info("Stage 4 repair: Successfully committed fixes for %d file(s).", len(fixed_files))
        return True
    except Exception as exc:
        logger.error("Stage 4 repair commit failed: %s", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Spec-Driven Development Pipeline — Orchestrator",
    )
    parser.add_argument(
        "ticket_id",
        nargs="?",
        default=None,
        help="Ticket ID/number to process in CLI mode (e.g. 1 or PROJ-123)",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Run the FastAPI webhook server and background queue worker",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and exit without running the pipeline",
    )
    parser.add_argument(
        "--debug-wire",
        action="store_true",
        help="Enable raw HTTP/socket transport debug logging (very verbose)",
    )
    parser.add_argument(
        "--blocking-gates",
        action="store_true",
        default=None,
        help="Wait synchronously for human gates (for CLI/dev use; overrides settings)",
    )
    parser.add_argument(
        "--non-blocking-gates",
        action="store_true",
        default=None,
        help="Do not wait synchronously for human gates; park pipeline and exit cleanly",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.debug_wire)

    settings = get_settings()
    if args.blocking_gates:
        settings.blocking_gates = True
    elif args.non_blocking_gates:
        settings.blocking_gates = False

    if args.server:
        import uvicorn
        logger.info(
            "Starting Webhook Server on %s:%d...",
            settings.webhook_host,
            settings.webhook_port,
        )
        uvicorn.run(
            "webhook.app:app",
            host=settings.webhook_host,
            port=settings.webhook_port,
            log_level="info",
        )
        return 0

    if not args.ticket_id:
        parser.error("Either ticket_id or --server must be specified.")

    return run_pipeline(
        ticket_id=args.ticket_id,
        dry_run=args.dry_run,
        settings=settings,
    )


if __name__ == "__main__":
    sys.exit(main())
