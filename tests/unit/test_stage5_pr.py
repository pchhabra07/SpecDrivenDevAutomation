"""
Unit tests for orchestrator.stages.stage5_pr (Phase 3).
"""

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.gate import GateOutcome, GateResult
from orchestrator.github_issue_client import GitHubIssueClient
from orchestrator.models import TicketData
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.stages.stage4_validation import GateStatus, ValidationResult
from orchestrator.stages.stage5_pr import Stage5Result, _build_mr_description, run


def _make_context(ticket_id: str = "PROJ-100") -> TicketContext:
    ticket = TicketData(
        ticket_id=ticket_id,
        summary="Fix user profile login redirection",
        description="When logging in, users should redirect to /dashboard",
        acceptance_criteria="Given a logged in user, when redirecting, target is /dashboard",
        labels=["SpecDrivenDev", "Frontend"],
        raw={"self": "https://jira.test/issue/PROJ-100"},
    )
    return TicketContext(
        ticket=ticket,
        default_branch="main",
        source_files={"src/auth/login.ts": "// login logic"},
        test_files={"tests/auth.test.ts": "// tests"},
        all_file_paths=["src/auth/login.ts", "tests/auth.test.ts"],
    )


def _make_validation_result() -> ValidationResult:
    return ValidationResult(
        pipeline_id=5001,
        pipeline_url="https://gitlab.test/pipelines/5001",
        overall_status="passed",
        gates=[
            GateStatus(name="lint", status="success"),
            GateStatus(name="sast", status="success"),
            GateStatus(name="dependency-scan", status="success"),
            GateStatus(name="test-coverage", status="success"),
            GateStatus(name="quality-gate", status="success"),
        ],
    )


class TestBuildMRDescription:
    def test_contains_required_sections(self):
        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(
            ticket_id="PROJ-100",
            iteration_count=2,
            modified_files=["src/auth/login.ts"],
            last_test_results={"passed": 4, "failed": 0},
        )

        desc = _build_mr_description(ctx, val, cp)

        assert "PROJ-100" in desc
        assert "Fix user profile login redirection" in desc
        assert "src/auth/login.ts" in desc
        assert "4/4 passed" in desc
        assert "| `lint` | ✅ PASS | Success |" in desc
        assert "https://gitlab.test/pipelines/5001" in desc
        assert "Auto-merge is strictly disabled per FR-20" in desc


class TestStage5Run:
    @patch("orchestrator.stages.stage5_pr.wait_for_approval")
    @patch("orchestrator.stages.stage5_pr.notify_gate2_pending")
    @patch("orchestrator.stages.stage5_pr.notify_gate2_approved")
    def test_run_approved_path(
        self, mock_notify_approved, mock_notify_pending, mock_wait
    ):
        mock_wait.return_value = GateResult(
            outcome=GateOutcome.APPROVED,
            approved_by=["reviewer1"],
            reviewer_comments=[],
        )

        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(ticket_id="PROJ-100", draft_mr_iid=42)
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        gitlab._project.web_url = "https://gitlab.test/project"
        jira = MagicMock(spec=GitHubIssueClient)

        result: Stage5Result = run(
            ticket_id="PROJ-100",
            context=ctx,
            validation_result=val,
            checkpoint=cp,
            audit=audit,
            gitlab=gitlab,
            jira=jira,
            gate_poll_interval=1,
            gate_timeout_hours=1.0,
        )

        assert result.approved is True
        assert result.status == "approved"
        assert result.approved_by == ["reviewer1"]

        # Verification of operations
        gitlab.promote_mr_to_ready.assert_called_once_with(42)
        gitlab.add_mr_label.assert_any_call(42, "ready-for-review")
        gitlab.update_mr_description.assert_called_once()
        assert cp.gate2_approved is True
        assert cp.gate2_mr_iid == 42
        mock_notify_approved.assert_called_once()
        audit.log.assert_any_call(
            stage=5,
            event_type="gate2_approved",
            payload={"mr_iid": 42, "approved_by": ["reviewer1"]},
        )

    @patch("orchestrator.stages.stage5_pr.wait_for_approval")
    @patch("orchestrator.stages.stage5_pr.notify_gate2_pending")
    @patch("orchestrator.stages.stage5_pr.notify_feedback_received")
    def test_run_changes_requested_path(
        self, mock_notify_feedback, mock_notify_pending, mock_wait
    ):
        mock_wait.return_value = GateResult(
            outcome=GateOutcome.CHANGES_REQUESTED,
            approved_by=[],
            reviewer_comments=["Please check boundary cases for null input."],
        )

        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(ticket_id="PROJ-100", draft_mr_iid=42)
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        gitlab._project.web_url = "https://gitlab.test/project"
        jira = MagicMock(spec=GitHubIssueClient)

        result: Stage5Result = run(
            ticket_id="PROJ-100",
            context=ctx,
            validation_result=val,
            checkpoint=cp,
            audit=audit,
            gitlab=gitlab,
            jira=jira,
            gate_poll_interval=1,
            gate_timeout_hours=1.0,
        )

        assert result.changes_requested is True
        assert result.approved is False
        assert "Please check boundary cases" in result.reviewer_feedback[0]

        gitlab.add_mr_label.assert_any_call(42, "changes-requested")
        assert cp.reviewer_feedback == ["Please check boundary cases for null input."]
        mock_notify_feedback.assert_called_once()
        audit.log.assert_any_call(
            stage=5,
            event_type="gate2_changes_requested",
            payload={"mr_iid": 42, "comments": ["Please check boundary cases for null input."]},
        )

    def test_run_raises_without_draft_mr(self):
        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(ticket_id="PROJ-100", draft_mr_iid=None)
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        jira = MagicMock(spec=GitHubIssueClient)

        with pytest.raises(ValueError, match="draft_mr_iid is not set"):
            run(
                ticket_id="PROJ-100",
                context=ctx,
                validation_result=val,
                checkpoint=cp,
                audit=audit,
                gitlab=gitlab,
                jira=jira,
            )
