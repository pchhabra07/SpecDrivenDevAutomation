"""
Stage 4 — CI/CD Validation Gates.

After Stage 3's coding loop produces code that passes the target tests,
this stage triggers the **full** CI/CD pipeline (all stages) and waits
for it to complete.  The pipeline runs ESLint, Semgrep, Trivy, Jest
with coverage, and optionally SonarQube.

Returns a ``ValidationResult`` consumed by Stage 5 (Phase 3) to
populate the final MR description.

Implements FR-12 through FR-16.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.ci_poller import CIResult, trigger_ci, wait_for_pipeline

logger = logging.getLogger(__name__)

# Expected CI job names across GitLab CI and GitHub Actions
_GATE_JOBS = ("lint", "sast", "dependency-scan", "test-coverage", "quality-gate")
_JOB_ALIASES: dict[str, tuple[str, ...]] = {
    "lint": ("lint", "eslint"),
    "sast": ("sast", "security-sast", "semgrep"),
    "dependency-scan": ("dependency-scan", "security-deps", "trivy"),
    "test-coverage": ("test-coverage", "test", "jest"),
    "quality-gate": ("quality-gate", "sonarqube", "sonarcloud"),
}


@dataclass
class GateStatus:
    """Result of a single CI validation gate."""

    name: str
    status: str  # "success", "failed", "skipped", "unknown"
    required: bool = True

    @property
    def passed(self) -> bool:
        return self.status == "success"


@dataclass
class ValidationResult:
    """Aggregated result of all CI/CD validation gates."""

    pipeline_id: int
    pipeline_url: str
    overall_status: str  # "passed", "failed"
    gates: list[GateStatus] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.overall_status == "passed"

    def to_dict(self) -> dict[str, Any]:
        """Serialise for checkpoint storage."""
        return {
            "pipeline_id": self.pipeline_id,
            "pipeline_url": self.pipeline_url,
            "overall_status": self.overall_status,
            "gates": [
                {"name": g.name, "status": g.status, "required": g.required}
                for g in self.gates
            ],
        }


def run(
    code_branch: str,
    gitlab: Any,
    checkpoint: Checkpoint,
    audit: AuditLogger,
    *,
    ci_poll_interval: int = 30,
    ci_timeout_minutes: int = 30,
    sonar_configured: bool = False,
    coverage_threshold: int = 80,
) -> ValidationResult:
    """
    Execute Stage 4: trigger full CI pipeline and evaluate gate results.

    Parameters
    ----------
    code_branch:
        The Git branch with the code changes from Stage 3.
    gitlab:
        Initialised ``GitHubClient`` or ``GitLabClient``.
    checkpoint:
        Current checkpoint (updated with pipeline ID and results).
    audit:
        Audit log writer.
    ci_poll_interval:
        Seconds between CI pipeline status polls.
    ci_timeout_minutes:
        Max minutes to wait for the pipeline.
    sonar_configured:
        Whether SonarQube is available.  If False, the ``quality-gate``
        job is treated as optional and skipped.
    coverage_threshold:
        Minimum Jest line-coverage percentage (for logging only — the
        actual enforcement is in the CI pipeline job itself).

    Returns
    -------
    ValidationResult
    """
    audit.log(stage=4, event_type="stage4_start")
    logger.info(
        "▶ Stage 4: Full CI validation on branch %s (coverage≥%d%%)",
        code_branch, coverage_threshold,
    )

    # ── 1. Trigger full CI pipeline ────────────────────────────────────
    pipeline_id = trigger_ci(gitlab, code_branch)
    checkpoint.ci_pipeline_id = pipeline_id
    audit.log(
        stage=4,
        event_type="ci_pipeline_triggered",
        payload={"pipeline_id": pipeline_id, "branch": code_branch},
    )

    # ── 2. Poll until terminal state ───────────────────────────────────
    ci_result = wait_for_pipeline(
        gitlab,
        pipeline_id=pipeline_id,
        poll_interval_seconds=ci_poll_interval,
        timeout_minutes=ci_timeout_minutes,
    )

    # ── 3. Evaluate individual gate statuses ───────────────────────────
    gates = _map_gate_statuses(ci_result, sonar_configured=sonar_configured)

    for gate in gates:
        audit.log(
            stage=4,
            event_type="ci_gate_result",
            payload={
                "gate": gate.name,
                "status": gate.status,
                "required": gate.required,
                "passed": gate.passed,
            },
        )
        logger.info(
            "  Gate %-18s: %s%s",
            gate.name,
            gate.status,
            "" if gate.required else " (optional)",
        )

    # ── 4. Determine overall pass/fail ─────────────────────────────────
    required_gates_passed = all(g.passed for g in gates if g.required)
    overall = "passed" if required_gates_passed else "failed"

    result = ValidationResult(
        pipeline_id=pipeline_id,
        pipeline_url=ci_result.web_url,
        overall_status=overall,
        gates=gates,
    )

    # Store in checkpoint for Stage 5.
    checkpoint.validation_result = result.to_dict()

    audit.log(
        stage=4,
        event_type="stage4_complete",
        payload={
            "overall_status": overall,
            "pipeline_url": ci_result.web_url,
        },
    )

    if result.passed:
        logger.info("✓ All validation gates passed.")
        checkpoint.current_stage = 5
    else:
        failed_gates = [g.name for g in gates if g.required and not g.passed]
        logger.warning(
            "✗ Validation FAILED — gates not passed: %s", failed_gates,
        )

    return result


def _map_gate_statuses(
    ci_result: CIResult,
    sonar_configured: bool,
) -> list[GateStatus]:
    """Map CI job results to structured gate statuses across GitLab and GitHub."""
    gates: list[GateStatus] = []

    for job_name in _GATE_JOBS:
        is_sonar = job_name == "quality-gate"
        required = not is_sonar or sonar_configured

        aliases = _JOB_ALIASES.get(job_name, (job_name,))
        job_status = None
        for alias in aliases:
            st = ci_result.job_status(alias)
            if st is not None:
                job_status = st
                break

        if job_status is None:
            if is_sonar and not sonar_configured:
                status = "skipped"
                logger.info(
                    "SonarQube not configured — skipping quality-gate check."
                )
            else:
                status = "unknown"
                logger.warning(
                    "CI job '%s' not found in pipeline results.", job_name,
                )
        else:
            status = job_status

        gates.append(GateStatus(
            name=job_name,
            status=status,
            required=required,
        ))

    return gates
