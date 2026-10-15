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
import sys
from typing import Any

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.config import Settings, get_settings
from orchestrator.gitlab_client import GitLabClient
from orchestrator.jira_client import JiraClient
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

    if settings.platform == "github" or (settings.github_token and settings.github_repo):
        from orchestrator.github_client import GitHubClient
        from orchestrator.github_issue_client import GitHubIssueClient

        jira = GitHubIssueClient(
            token=settings.github_token,
            repo=settings.github_repo,
            base_url=settings.github_base_url,
        )
        gitlab = GitHubClient(
            token=settings.github_token,
            repo=settings.github_repo,
            base_url=settings.github_base_url,
            workflow_file=settings.github_workflow_file or None,
        )
    else:
        jira = JiraClient(
            base_url=settings.jira_base_url,
            email=settings.jira_user_email,
            api_token=settings.jira_api_token,
        )
        gitlab = GitLabClient(
            base_url=settings.gitlab_base_url,
            private_token=settings.gitlab_api_token,
            repo_id=settings.gitlab_repo_id,
        )

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
                checkpoint = Checkpoint(ticket_id=ticket_id)
                checkpoint.repo_id = gitlab.project_id

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
        checkpoint = Checkpoint(ticket_id=ticket_id)
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
            )

        # ── Stage 2: Generate Tests + Gate 1 ────────────────────────────
        if checkpoint.current_stage <= 2 and not checkpoint.gate1_approved:
            logger.info("▶ Stage 2: Generate Test Cases + Human Gate 1")
            locked_tests = stage2_test_gen.run(
                context=context,
                gitlab=gitlab,
                llm=llm,
                checkpoint=checkpoint,
                audit=audit,
                gate_poll_interval=settings.gate_poll_interval_seconds,
                gate_timeout_hours=settings.gate_timeout_hours,
            )
            checkpoint.conversation_history = llm.history
            checkpoint.save(settings.checkpoint_dir)
            logger.info(
                "Stage 2 complete — %d test file(s) locked.",
                len(locked_tests),
            )

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
                )
                checkpoint.save(settings.checkpoint_dir)

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
            )
            checkpoint.save(settings.checkpoint_dir)

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
                logger.info(
                    "Reviewer requested changes (cycle %d/%d). Re-routing to Stage 3.",
                    checkpoint.review_iteration_count,
                    settings.max_review_iterations,
                )

                # Phase 2: Expand context with files referenced by reviewer.
                logger.info("Phase 2: Expanding context for reviewer feedback…")
                refresh_context_for_feedback(
                    reviewer_feedback=stage5_result.reviewer_feedback,
                    context=context,
                    gitlab=gitlab,
                    audit=audit,
                )

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
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.debug_wire)

    settings = get_settings()

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
