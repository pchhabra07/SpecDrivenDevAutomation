"""Unit tests for orchestrator.stages.stage4_validation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.ci_poller import CIResult
from orchestrator.stages.stage4_validation import (
    GateStatus,
    ValidationResult,
    run,
    _map_gate_statuses,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_checkpoint() -> Checkpoint:
    cp = Checkpoint(ticket_id="PROJ-42")
    cp.current_stage = 4
    cp.code_branch = "automation/PROJ-42"
    return cp


def _success_ci_result() -> CIResult:
    """All gates pass."""
    return CIResult(
        pipeline_id=1,
        status="success",
        web_url="https://gitlab.com/pipelines/1",
        jobs=[
            {"name": "lint", "status": "success"},
            {"name": "sast", "status": "success"},
            {"name": "dependency-scan", "status": "success"},
            {"name": "test-coverage", "status": "success"},
            {"name": "quality-gate", "status": "success"},
        ],
    )


def _partial_failure_ci_result() -> CIResult:
    """Lint and SAST fail."""
    return CIResult(
        pipeline_id=2,
        status="failed",
        web_url="https://gitlab.com/pipelines/2",
        jobs=[
            {"name": "lint", "status": "failed"},
            {"name": "sast", "status": "failed"},
            {"name": "dependency-scan", "status": "success"},
            {"name": "test-coverage", "status": "success"},
            {"name": "quality-gate", "status": "success"},
        ],
    )


# ── Gate Status Mapping ────────────────────────────────────────────────


class TestMapGateStatuses:
    """Tests for ``_map_gate_statuses``."""

    def test_all_gates_present_with_sonar(self):
        ci = _success_ci_result()
        gates = _map_gate_statuses(ci, sonar_configured=True)

        assert len(gates) == 5
        assert all(g.passed for g in gates)
        assert all(g.required for g in gates)

    def test_sonar_skipped_when_not_configured(self):
        ci = CIResult(
            pipeline_id=3, status="success", web_url="",
            jobs=[
                {"name": "lint", "status": "success"},
                {"name": "sast", "status": "success"},
                {"name": "dependency-scan", "status": "success"},
                {"name": "test-coverage", "status": "success"},
                # No quality-gate job.
            ],
        )
        gates = _map_gate_statuses(ci, sonar_configured=False)

        quality_gate = next(g for g in gates if g.name == "quality-gate")
        assert quality_gate.status == "skipped"
        assert quality_gate.required is False

    def test_missing_job_returns_unknown(self):
        ci = CIResult(
            pipeline_id=4, status="failed", web_url="",
            jobs=[{"name": "lint", "status": "success"}],
        )
        gates = _map_gate_statuses(ci, sonar_configured=True)

        sast = next(g for g in gates if g.name == "sast")
        assert sast.status == "unknown"

    def test_partial_failure(self):
        ci = _partial_failure_ci_result()
        gates = _map_gate_statuses(ci, sonar_configured=True)

        lint = next(g for g in gates if g.name == "lint")
        sast = next(g for g in gates if g.name == "sast")
        assert lint.passed is False
        assert sast.passed is False

        dep_scan = next(g for g in gates if g.name == "dependency-scan")
        assert dep_scan.passed is True


# ── ValidationResult ───────────────────────────────────────────────────


class TestValidationResult:
    """Tests for ``ValidationResult`` dataclass."""

    def test_passed(self):
        result = ValidationResult(
            pipeline_id=1, pipeline_url="", overall_status="passed",
        )
        assert result.passed is True

    def test_failed(self):
        result = ValidationResult(
            pipeline_id=1, pipeline_url="", overall_status="failed",
        )
        assert result.passed is False

    def test_to_dict(self):
        gates = [GateStatus(name="lint", status="success", required=True)]
        result = ValidationResult(
            pipeline_id=1, pipeline_url="url", overall_status="passed",
            gates=gates,
        )
        d = result.to_dict()
        assert d["pipeline_id"] == 1
        assert d["pipeline_url"] == "url"
        assert len(d["gates"]) == 1
        assert d["gates"][0]["name"] == "lint"


# ── Full Run ────────────────────────────────────────────────────────────


class TestStage4Run:
    """Integration-level tests for the Stage 4 ``run`` function."""

    @patch("orchestrator.stages.stage4_validation.wait_for_pipeline")
    @patch("orchestrator.stages.stage4_validation.trigger_ci")
    def test_all_gates_pass(self, mock_trigger, mock_wait):
        """All CI gates pass → ValidationResult.passed is True, stage→5."""
        mock_trigger.return_value = 1
        mock_wait.return_value = CIResult(
            pipeline_id=1, status="success",
            web_url="https://gitlab.com/pipelines/1",
            jobs=[
                {"name": "lint", "status": "success"},
                {"name": "sast", "status": "success"},
                {"name": "dependency-scan", "status": "success"},
                {"name": "test-coverage", "status": "success"},
            ],
        )

        gitlab = MagicMock()
        checkpoint = _make_checkpoint()
        audit = MagicMock(spec=AuditLogger)

        result = run(
            code_branch="automation/PROJ-42",
            gitlab=gitlab,
            checkpoint=checkpoint,
            audit=audit,
            ci_poll_interval=1,
            ci_timeout_minutes=1,
            sonar_configured=False,
        )

        assert result.passed is True
        assert checkpoint.current_stage == 5
        assert checkpoint.validation_result["overall_status"] == "passed"

    @patch("orchestrator.stages.stage4_validation.wait_for_pipeline")
    @patch("orchestrator.stages.stage4_validation.trigger_ci")
    def test_gate_failure(self, mock_trigger, mock_wait):
        """A required gate fails → ValidationResult.passed is False."""
        mock_trigger.return_value = 2
        mock_wait.return_value = CIResult(
            pipeline_id=2, status="failed",
            web_url="https://gitlab.com/pipelines/2",
            jobs=[
                {"name": "lint", "status": "failed"},
                {"name": "sast", "status": "success"},
                {"name": "dependency-scan", "status": "success"},
                {"name": "test-coverage", "status": "success"},
            ],
        )

        gitlab = MagicMock()
        checkpoint = _make_checkpoint()
        audit = MagicMock(spec=AuditLogger)

        result = run(
            code_branch="automation/PROJ-42",
            gitlab=gitlab,
            checkpoint=checkpoint,
            audit=audit,
            ci_poll_interval=1,
            ci_timeout_minutes=1,
            sonar_configured=False,
        )

        assert result.passed is False
        # Stage should NOT advance to 5 on failure.
        assert checkpoint.current_stage == 4

    @patch("orchestrator.stages.stage4_validation.wait_for_pipeline")
    @patch("orchestrator.stages.stage4_validation.trigger_ci")
    def test_audit_logging(self, mock_trigger, mock_wait):
        """Should log ci_gate_result for each gate and stage4_complete."""
        mock_trigger.return_value = 3
        mock_wait.return_value = CIResult(
            pipeline_id=3, status="success",
            web_url="https://gitlab.com/pipelines/3",
            jobs=[
                {"name": "lint", "status": "success"},
                {"name": "sast", "status": "success"},
                {"name": "dependency-scan", "status": "success"},
                {"name": "test-coverage", "status": "success"},
            ],
        )

        gitlab = MagicMock()
        checkpoint = _make_checkpoint()
        audit = MagicMock(spec=AuditLogger)

        run(
            code_branch="automation/PROJ-42",
            gitlab=gitlab,
            checkpoint=checkpoint,
            audit=audit,
            ci_poll_interval=1,
            ci_timeout_minutes=1,
            sonar_configured=False,
        )

        # Should have audit calls: stage4_start, ci_pipeline_triggered,
        # one ci_gate_result per gate, stage4_complete.
        event_types = [c.kwargs.get("event_type", c.args[1] if len(c.args) > 1 else "")
                       for c in audit.log.call_args_list]
        assert "stage4_start" in event_types
        assert "stage4_complete" in event_types
        assert event_types.count("ci_gate_result") == 5  # 5 gates (including optional sonar)
