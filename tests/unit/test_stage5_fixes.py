"""
Comprehensive unit tests for Stage 5 fixes and logical edge cases:
- S5-1: Non-blocking Gate 2 (check_approval_once, GatePendingError, checkpoint parking, webhook resume)
- S5-1: Stale gate monitor & timeout handling
- S5-1: Idempotent PR promotion and notifications on re-entry (Edge Case 1)
- S5-2: Canonical PR/MR URL resolution (get_pr_url) without private attribute snooping
- S5-3: Formal GitHub review states vs comment substring matching
- S5-4: Formal Protocol / ABC interfaces (VCSClient, IssueTrackerClient) and canonical aliases
"""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.config import Settings
from orchestrator.exceptions import GatePendingError
from orchestrator.gate import GateOutcome, GateResult
from orchestrator.github_client import GitHubClient
from orchestrator.github_issue_client import GitHubIssueClient
from orchestrator.models import TicketData
from orchestrator.main import run_pipeline
from orchestrator.protocols import IssueTrackerClient, VCSClient
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.stages.stage4_validation import GateStatus, ValidationResult
from orchestrator.stages.stage5_pr import Stage5Result, run as stage5_run
from webhook.app import _stale_gate_monitor, task_queue


def _make_ticket(
    ticket_id: str = "PROJ-200",
    summary: str = "Implement OAuth2 Token Revocation",
    description: str = "Add revocation endpoint per RFC 7009",
    acceptance_criteria: str = "Revoking a valid token returns 200 OK",
    labels: list[str] | None = None,
) -> TicketData:
    return TicketData(
        ticket_id=ticket_id,
        summary=summary,
        description=description,
        acceptance_criteria=acceptance_criteria,
        labels=labels or ["SpecDrivenDev"],
        raw={"self": "https://jira.test/issue/PROJ-200"},
    )


def _make_context(ticket_id: str = "PROJ-200") -> TicketContext:
    return TicketContext(
        ticket=_make_ticket(ticket_id=ticket_id),
        default_branch="main",
        source_files={"src/auth/revoke.py": "def revoke(): pass"},
        test_files={"tests/test_revoke.py": "def test_revoke(): pass"},
        all_file_paths=["src/auth/revoke.py", "tests/test_revoke.py"],
    )


def _make_validation_result() -> ValidationResult:
    return ValidationResult(
        pipeline_id=6001,
        pipeline_url="https://github.com/org/repo/actions/runs/6001",
        overall_status="passed",
        gates=[
            GateStatus(name="lint", status="success"),
            GateStatus(name="sast", status="success"),
            GateStatus(name="dependency-scan", status="success"),
            GateStatus(name="test-coverage", status="success"),
            GateStatus(name="quality-gate", status="success"),
        ],
    )


class TestS5_1_NonBlockingGate2:
    """Tests for S5-1: Non-blocking Gate 2 suspension, checkpointing, and resume."""

    @patch("orchestrator.stages.stage5_pr.check_approval_once")
    @patch("orchestrator.stages.stage5_pr.notify_gate2_pending")
    def test_non_blocking_gate2_pending_suspends_pipeline(
        self, mock_notify_pending, mock_check_once, tmp_path
    ):
        mock_check_once.return_value = GateResult(
            outcome=GateOutcome.PENDING,
            approved_by=[],
            reviewer_comments=[],
        )

        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(ticket_id="PROJ-200", draft_mr_iid=77, checkpoint_dir=str(tmp_path))
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        gitlab.get_pr_url.return_value = "https://github.com/org/repo/pull/77"
        jira = MagicMock(spec=GitHubIssueClient)

        with pytest.raises(GatePendingError) as exc_info:
            stage5_run(
                ticket_id="PROJ-200",
                context=ctx,
                validation_result=val,
                checkpoint=cp,
                audit=audit,
                gitlab=gitlab,
                jira=jira,
                blocking_gate=False,
                checkpoint_dir=str(tmp_path),
            )

        assert exc_info.value.gate_name == "gate2"
        assert exc_info.value.mr_iid == 77
        assert cp.gate_waiting == "gate2"
        assert cp.gate_waiting_mr_iid == 77
        assert cp.gate_waiting_since is not None
        assert cp.final_mr_url == "https://github.com/org/repo/pull/77"

        # Checkpoint should be saved on disk
        saved = Checkpoint.load("PROJ-200", str(tmp_path))
        assert saved is not None
        assert saved.gate_waiting == "gate2"
        assert saved.gate_waiting_mr_iid == 77

    @patch("orchestrator.stages.stage5_pr.check_approval_once")
    @patch("orchestrator.stages.stage5_pr.notify_gate2_approved")
    @patch("orchestrator.stages.stage5_pr.notify_pipeline_complete")
    def test_non_blocking_gate2_approval_clears_waiting_state(
        self, mock_notify_complete, mock_notify_approved, mock_check_once, tmp_path
    ):
        mock_check_once.return_value = GateResult(
            outcome=GateOutcome.APPROVED,
            approved_by=["senior-reviewer"],
            reviewer_comments=[],
        )

        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(
            ticket_id="PROJ-200",
            draft_mr_iid=77,
            gate_waiting="gate2",
            gate_waiting_mr_iid=77,
            gate_waiting_since=time.time() - 100,
            checkpoint_dir=str(tmp_path),
        )
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        gitlab.get_pr_url.return_value = "https://github.com/org/repo/pull/77"
        jira = MagicMock(spec=GitHubIssueClient)

        result = stage5_run(
            ticket_id="PROJ-200",
            context=ctx,
            validation_result=val,
            checkpoint=cp,
            audit=audit,
            gitlab=gitlab,
            jira=jira,
            blocking_gate=False,
            checkpoint_dir=str(tmp_path),
        )

        assert result.approved is True
        assert result.approved_by == ["senior-reviewer"]
        assert cp.gate2_approved is True
        assert cp.gate_waiting is None
        assert cp.gate_waiting_mr_iid is None
        assert cp.gate_waiting_since is None

    @patch("orchestrator.stages.stage5_pr.check_approval_once")
    @patch("orchestrator.stages.stage5_pr.notify_feedback_received")
    def test_non_blocking_gate2_changes_requested_clears_waiting_and_routes(
        self, mock_notify_feedback, mock_check_once, tmp_path
    ):
        mock_check_once.return_value = GateResult(
            outcome=GateOutcome.CHANGES_REQUESTED,
            approved_by=[],
            reviewer_comments=["Add check for expired refresh token."],
        )

        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(
            ticket_id="PROJ-200",
            draft_mr_iid=77,
            gate_waiting="gate2",
            gate_waiting_mr_iid=77,
            gate_waiting_since=time.time() - 100,
            checkpoint_dir=str(tmp_path),
        )
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        gitlab.get_pr_url.return_value = "https://github.com/org/repo/pull/77"
        jira = MagicMock(spec=GitHubIssueClient)

        result = stage5_run(
            ticket_id="PROJ-200",
            context=ctx,
            validation_result=val,
            checkpoint=cp,
            audit=audit,
            gitlab=gitlab,
            jira=jira,
            blocking_gate=False,
            checkpoint_dir=str(tmp_path),
        )

        assert result.changes_requested is True
        assert result.reviewer_feedback == ["Add check for expired refresh token."]
        assert cp.reviewer_feedback == ["Add check for expired refresh token."]
        assert cp.gate_waiting is None
        assert cp.gate_waiting_mr_iid is None
        assert cp.gate_waiting_since is None
        # Promotion iteration tracker must be reset so next cycle updates PR
        assert cp.gate2_promoted_iteration is None

    @patch("orchestrator.main.stage5_pr.run")
    def test_main_handles_gate2_pending_and_parks_ticket(self, mock_stage5_run, tmp_path):
        """Verify main.py catches GatePendingError at Stage 5, logs audit, and returns 0."""
        mock_stage5_run.side_effect = GatePendingError("gate2", 77)

        cp = Checkpoint(
            ticket_id="PROJ-200",
            current_stage=5,
            gate1_approved=True,
            draft_mr_iid=77,
            checkpoint_dir=str(tmp_path),
        )
        cp.save(str(tmp_path))

        settings = Settings(
            nim_api_key="test-key",
            nim_base_url="https://nim.test",
            checkpoint_dir=str(tmp_path),
            audit_dir=str(tmp_path / "audit"),
            blocking_gates=False,
            github_token="ghp_test",
            github_repo="org/repo",
        )

        with patch("orchestrator.main.stage1_context.run") as mock_stage1:
            mock_stage1.return_value = _make_context()
            with patch("orchestrator.main.GitHubIssueClient"):
                with patch("orchestrator.main.GitHubClient"):
                    with patch("orchestrator.main.LLMClient"):
                        code = run_pipeline("PROJ-200", settings=settings)

        assert code == 0


class TestS5_1_StaleGateTimeoutAndIdempotency:
    """Tests for Gate 2 timeout, stale-gate monitor, and re-entry idempotency."""

    @patch("orchestrator.stages.stage5_pr.check_approval_once")
    def test_non_blocking_gate2_detects_timeout_when_elapsed(
        self, mock_check_once, tmp_path
    ):
        """Edge Case 2: If ticket was waiting longer than gate_timeout_hours, return status='timeout'."""
        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(
            ticket_id="PROJ-200",
            draft_mr_iid=77,
            gate_waiting="gate2",
            gate_waiting_mr_iid=77,
            gate_waiting_since=time.time() - (49 * 3600),  # 49 hours ago (timeout is 48)
            checkpoint_dir=str(tmp_path),
        )
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        gitlab.get_pr_url.return_value = "https://github.com/org/repo/pull/77"
        jira = MagicMock(spec=GitHubIssueClient)

        result = stage5_run(
            ticket_id="PROJ-200",
            context=ctx,
            validation_result=val,
            checkpoint=cp,
            audit=audit,
            gitlab=gitlab,
            jira=jira,
            gate_timeout_hours=48.0,
            blocking_gate=False,
            checkpoint_dir=str(tmp_path),
        )

        assert result.status == "timeout"
        assert cp.gate_waiting is None
        assert cp.gate_waiting_since is None
        # Should not have called check_approval_once because it already timed out
        mock_check_once.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_gate_monitor_reenqueues_expired_checkpoints(self, tmp_path):
        """Verify _stale_gate_monitor finds checkpoints waiting past timeout and re-enqueues."""
        while not task_queue.empty():
            task_queue.get_nowait()

        cp_file = tmp_path / "PROJ-TIMEOUT.json"
        cp_file.write_text(
            json.dumps({
                "ticket_id": "PROJ-TIMEOUT",
                "gate_waiting": "gate2",
                "gate_waiting_since": time.time() - 200000,  # ~55h ago
            }),
            encoding="utf-8",
        )

        # Write another checkpoint that is NOT expired
        cp_active = tmp_path / "PROJ-ACTIVE.json"
        cp_active.write_text(
            json.dumps({
                "ticket_id": "PROJ-ACTIVE",
                "gate_waiting": "gate2",
                "gate_waiting_since": time.time() - 3600,  # 1h ago
            }),
            encoding="utf-8",
        )

        settings_mock = MagicMock()
        settings_mock.checkpoint_dir = str(tmp_path)
        settings_mock.gate_timeout_hours = 48.0

        with patch("webhook.app.get_settings", return_value=settings_mock):
            # Run one cycle of _stale_gate_monitor
            monitor_task = asyncio.create_task(_stale_gate_monitor(poll_interval=0.01))
            await asyncio.sleep(0.05)
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        queued = []
        while not task_queue.empty():
            queued.append(await task_queue.get())

        assert "PROJ-TIMEOUT" in queued
        assert "PROJ-ACTIVE" not in queued

    @patch("orchestrator.stages.stage5_pr.check_approval_once")
    @patch("orchestrator.stages.stage5_pr.notify_gate2_pending")
    def test_reentry_skips_duplicate_pr_promotion_and_notifications(
        self, mock_notify_pending, mock_check_once, tmp_path
    ):
        """Edge Case 1: When waking up from suspension, do not spam reviewers with duplicate emails/PR edits."""
        mock_check_once.return_value = GateResult(
            outcome=GateOutcome.PENDING,
            approved_by=[],
            reviewer_comments=[],
        )

        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(
            ticket_id="PROJ-200",
            draft_mr_iid=77,
            iteration_count=1,
            gate2_promoted_iteration=1,  # Already promoted for iteration 1!
            checkpoint_dir=str(tmp_path),
        )
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        gitlab.get_pr_url.return_value = "https://github.com/org/repo/pull/77"
        jira = MagicMock(spec=GitHubIssueClient)

        with pytest.raises(GatePendingError):
            stage5_run(
                ticket_id="PROJ-200",
                context=ctx,
                validation_result=val,
                checkpoint=cp,
                audit=audit,
                gitlab=gitlab,
                jira=jira,
                blocking_gate=False,
                checkpoint_dir=str(tmp_path),
            )

        # Promotion and pending notifications should be SKIPPED
        gitlab.promote_mr_to_ready.assert_not_called()
        gitlab.update_mr_description.assert_not_called()
        mock_notify_pending.assert_not_called()
        jira.post_lifecycle_comment.assert_not_called()


class TestS5_3_FormalReviewStates:
    """Tests for S5-3: Strict formal GitHub review states vs naive comment substring matching."""

    @patch("orchestrator.github_client.Github")
    def test_comment_mentioning_changes_requested_does_not_trigger_rejection(self, mock_gh_cls):
        """Conversational remark mentioning 'changes requested' must not trigger changes_requested."""
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_pr = MagicMock()
        # Reviewer approved formally
        rev = MagicMock()
        rev.state = "APPROVED"
        rev.user.login = "charlie"
        mock_pr.get_reviews.return_value = [rev]
        mock_repo.get_pull.return_value = mock_pr

        # Issue comment with "changes requested" in conversation
        comm = MagicMock()
        comm.body = "I notice changes were requested on the previous PR too, but this looks great!"
        comm.user.login = "reviewer-bob"
        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = [comm]
        mock_repo.get_issue.return_value = mock_issue

        client = GitHubClient(token="ghp_test", repo="org/repo")
        approvals = client.get_pr_approvals(77)

        assert approvals["approved"] is True
        assert approvals["approved_by"] == ["charlie"]
        assert approvals["changes_requested"] is False

    @patch("orchestrator.github_client.Github")
    def test_comment_mentioning_lgtm_is_not_formal_approval(self, mock_gh_cls):
        """Conversational 'LGTM' in an issue comment is NOT formal approval without formal review."""
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_pr = MagicMock()
        mock_pr.get_reviews.return_value = []  # No formal reviews
        mock_repo.get_pull.return_value = mock_pr

        comm = MagicMock()
        comm.body = "LGTM! Looks good to me."
        comm.user.login = "peer"
        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = [comm]
        mock_repo.get_issue.return_value = mock_issue

        client = GitHubClient(token="ghp_test", repo="org/repo")
        approvals = client.get_pr_approvals(77)

        assert approvals["approved"] is False
        assert approvals["approved_by"] == []

    @patch("orchestrator.github_client.Github")
    def test_latest_review_per_author_precedence(self, mock_gh_cls):
        """If author first requested changes, then approved, latest review state (APPROVED) wins."""
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_pr = MagicMock()
        rev1 = MagicMock()
        rev1.state = "CHANGES_REQUESTED"
        rev1.user.login = "alice"

        rev2 = MagicMock()
        rev2.state = "APPROVED"
        rev2.user.login = "alice"

        mock_pr.get_reviews.return_value = [rev1, rev2]
        mock_repo.get_pull.return_value = mock_pr

        client = GitHubClient(token="ghp_test", repo="org/repo")
        approvals = client.get_pr_approvals(77)

        assert approvals["approved"] is True
        assert approvals["approved_by"] == ["alice"]
        assert approvals["changes_requested"] is False

    @patch("orchestrator.github_client.Github")
    def test_commented_review_does_not_clear_changes_requested(self, mock_gh_cls):
        """If author requested changes, then submitted a COMMENTED review, CHANGES_REQUESTED is retained."""
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_pr = MagicMock()
        rev1 = MagicMock()
        rev1.state = "CHANGES_REQUESTED"
        rev1.user.login = "alice"

        rev2 = MagicMock()
        rev2.state = "COMMENTED"
        rev2.user.login = "alice"

        mock_pr.get_reviews.return_value = [rev1, rev2]
        mock_repo.get_pull.return_value = mock_pr

        client = GitHubClient(token="ghp_test", repo="org/repo")
        approvals = client.get_pr_approvals(77)

        assert approvals["changes_requested"] is True
        assert approvals["approved"] is False

    @patch("orchestrator.github_client.Github")
    def test_dismissed_review_clears_decision(self, mock_gh_cls):
        """If author's CHANGES_REQUESTED review is DISMISSED, changes_requested becomes False."""
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_pr = MagicMock()
        rev1 = MagicMock()
        rev1.state = "CHANGES_REQUESTED"
        rev1.user.login = "alice"

        rev2 = MagicMock()
        rev2.state = "DISMISSED"
        rev2.user.login = "alice"

        mock_pr.get_reviews.return_value = [rev1, rev2]
        mock_repo.get_pull.return_value = mock_pr

        client = GitHubClient(token="ghp_test", repo="org/repo")
        approvals = client.get_pr_approvals(77)

        assert approvals["changes_requested"] is False
        assert approvals["approved"] is False


class TestS5_2_CanonicalPRUrl:
    """Tests for S5-2: Canonical PR/MR URL lookup."""

    @patch("orchestrator.github_client.Github")
    def test_github_client_get_pr_url(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_repo.html_url = "https://github.com/my-org/my-project"
        mock_gh.get_repo.return_value = mock_repo

        client = GitHubClient(token="ghp_test", repo="my-org/my-project")
        url = client.get_pr_url(123)
        assert url == "https://github.com/my-org/my-project/pull/123"

    @patch("orchestrator.github_client.Github")
    def test_github_client_conforms_to_vcs_protocol(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        client = GitHubClient(token="ghp_test", repo="org/repo")
        assert isinstance(client, VCSClient)

    @patch("orchestrator.github_issue_client.Github")
    def test_github_issue_client_conforms_to_issue_tracker_protocol(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        client = GitHubIssueClient(token="ghp_test", repo="org/repo")
        assert isinstance(client, IssueTrackerClient)

    @patch("orchestrator.stages.stage5_pr.check_approval_once")
    @patch("orchestrator.stages.stage5_pr.notify_feedback_received")
    def test_changes_requested_empty_comments_fallback(
        self, mock_notify_feedback, mock_check_once, tmp_path
    ):
        """Edge Case 5: When changes requested without inline comments, provide fallback note."""
        mock_check_once.return_value = GateResult(
            outcome=GateOutcome.CHANGES_REQUESTED,
            approved_by=[],
            reviewer_comments=[],  # Empty comments
        )

        ctx = _make_context()
        val = _make_validation_result()
        cp = Checkpoint(ticket_id="PROJ-200", draft_mr_iid=77, checkpoint_dir=str(tmp_path))
        audit = MagicMock(spec=AuditLogger)
        gitlab = MagicMock()
        jira = MagicMock(spec=GitHubIssueClient)

        result = stage5_run(
            ticket_id="PROJ-200",
            context=ctx,
            validation_result=val,
            checkpoint=cp,
            audit=audit,
            gitlab=gitlab,
            jira=jira,
            blocking_gate=False,
            checkpoint_dir=str(tmp_path),
        )

        assert result.changes_requested is True
        assert len(result.reviewer_feedback) == 1
        assert "Reviewer requested changes" in result.reviewer_feedback[0]
        assert cp.reviewer_feedback == result.reviewer_feedback

    @patch("orchestrator.github_client.Github")
    def test_github_client_get_pr_url_fallback(self, mock_gh_cls):
        """S5-2: When repo attribute is missing or broken, return safe fallback."""
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        del mock_repo.html_url  # Cause AttributeError
        mock_gh.get_repo.return_value = mock_repo

        client = GitHubClient(token="ghp_test", repo="org/repo")
        url = client.get_pr_url(99)
        assert url == "https://github.com/pull/99"

    @patch("orchestrator.main.stage5_pr.run")
    def test_main_handles_gate2_timeout_and_returns_1(self, mock_stage5_run, tmp_path):
        """Edge Case 2: When Stage 5 returns status='timeout', main.py comments and returns 1."""
        mock_stage5_run.return_value = Stage5Result(
            mr_iid=77,
            mr_url="https://github.com/org/repo/pull/77",
            status="timeout",
        )

        cp = Checkpoint(
            ticket_id="PROJ-200",
            current_stage=5,
            gate1_approved=True,
            draft_mr_iid=77,
            checkpoint_dir=str(tmp_path),
        )
        cp.save(str(tmp_path))

        settings = Settings(
            nim_api_key="test-key",
            nim_base_url="https://nim.test",
            checkpoint_dir=str(tmp_path),
            audit_dir=str(tmp_path / "audit"),
            blocking_gates=False,
            github_token="ghp_test",
            github_repo="org/repo",
        )

        with patch("orchestrator.main.stage1_context.run") as mock_stage1:
            mock_stage1.return_value = _make_context()
            with patch("orchestrator.main.GitHubIssueClient") as mock_jira_cls:
                mock_jira = mock_jira_cls.return_value
                with patch("orchestrator.main.GitHubClient"):
                    with patch("orchestrator.main.LLMClient"):
                        code = run_pipeline("PROJ-200", settings=settings)

        assert code == 1
        mock_jira.post_lifecycle_comment.assert_called_once()
        assert "Gate 2 Approval Timeout" in mock_jira.post_lifecycle_comment.call_args[1]["title"]

