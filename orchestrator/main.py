"""
Orchestrator entry point — CLI-driven for Phase 1.

Accepts a JIRA ticket ID, runs Stage 1 (context assembly) and Stage 2
(test generation + Gate 1 approval), and saves the checkpoint after
each stage transition.

Phase 2 will wire in Stage 3–5.  Phase 3 will add a webhook listener
as an alternative to the CLI trigger.
"""

from __future__ import annotations

import argparse
import logging
import sys

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.config import get_settings
from orchestrator.gitlab_client import GitLabClient
from orchestrator.jira_client import JiraClient
from orchestrator.llm_client import LLMClient
from orchestrator.rate_limiter import RateLimiter
from orchestrator.stages import stage1_context, stage2_test_gen

logger = logging.getLogger("orchestrator")

_SYSTEM_PROMPT = (
    "You are a spec-driven development agent.  Your job is to analyse JIRA "
    "tickets for a JavaScript/TypeScript/React/Redux codebase, generate Jest "
    "test cases from the ticket's acceptance criteria, and later implement "
    "code changes that make those tests pass.  Always follow the file-path "
    "output format specified in each prompt."
)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(name)-28s  %(levelname)-5s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Spec-Driven Development Pipeline — Phase 1 CLI",
    )
    parser.add_argument(
        "ticket_id",
        help="JIRA ticket key to process (e.g. PROJ-123)",
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
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    ticket_id: str = args.ticket_id
    logger.info("═" * 60)
    logger.info("  Spec-Driven Pipeline — Processing %s", ticket_id)
    logger.info("═" * 60)

    # ── Initialise shared components ────────────────────────────────────
    settings = get_settings()
    rate_limiter = RateLimiter(max_calls=settings.rate_limit_cap)

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
    )

    audit = AuditLogger(ticket_id=ticket_id, audit_dir=settings.audit_dir)

    # ── Checkpoint: resume or create ────────────────────────────────────
    checkpoint = Checkpoint.load(ticket_id, settings.checkpoint_dir)
    if checkpoint:
        logger.info("Resuming from checkpoint (stage=%d, iter=%d)",
                     checkpoint.current_stage, checkpoint.iteration_count)
        # Restore LLM conversation history from checkpoint.
        if checkpoint.conversation_history:
            llm.set_history(checkpoint.conversation_history)
    else:
        checkpoint = Checkpoint(ticket_id=ticket_id)
        logger.info("No checkpoint found — starting fresh.")

    audit.log(stage=0, event_type="pipeline_start", payload={"resumed": checkpoint.current_stage > 1})

    if args.dry_run:
        logger.info("Dry-run mode — configuration validated. Exiting.")
        return 0

    try:
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
            )
            # Persist conversation history + checkpoint.
            checkpoint.conversation_history = llm.history
            checkpoint.save(settings.checkpoint_dir)
        else:
            # Reconstruct context from checkpoint if we're resuming past Stage 1.
            # In Phase 1, a full re-fetch is cheap and ensures freshness.
            logger.info("▶ Stage 1: Re-fetching context (resuming from Stage %d)",
                         checkpoint.current_stage)
            context = stage1_context.run(
                ticket_id=ticket_id,
                jira=jira,
                gitlab=gitlab,
                llm=llm,
                checkpoint=checkpoint,
                audit=audit,
                qualifying_label=settings.qualifying_label,
            )

        # ── Stage 2: Generate Tests + Gate 1 ────────────────────────────
        if checkpoint.current_stage <= 2:
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
                "Stage 2 complete — %d test file(s) locked. "
                "Waiting for Phase 2 (code generation) to be wired in.",
                len(locked_tests),
            )

        # ── Stages 3–5 ─────────────────────────────────────────────────
        if checkpoint.current_stage >= 3:
            logger.info(
                "⏸  Stages 3–5 are not yet implemented (Phase 2). "
                "Pipeline paused at Stage %d.",
                checkpoint.current_stage,
            )

        audit.log(stage=0, event_type="pipeline_complete")
        logger.info("Pipeline finished for %s.", ticket_id)
        return 0

    except Exception:
        logger.exception("Pipeline failed for %s", ticket_id)
        audit.log(stage=checkpoint.current_stage, event_type="pipeline_error")
        # Save checkpoint so we can resume after the error is resolved.
        checkpoint.conversation_history = llm.history
        checkpoint.save(settings.checkpoint_dir)
        return 1


if __name__ == "__main__":
    sys.exit(main())
