"""
Unit tests for orchestrator.gate.
"""

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.gate import (
    GateOutcome,
    GateResult,
    GateTimeoutError,
    wait_for_approval,
)


class TestWaitForApproval:
    """Tests for the shared approval-detection logic."""

    def test_approved_on_first_poll(self):
        """If the MR is already approved, return immediately."""
        mock_gl = MagicMock()
        mock_gl.get_mr_approvals.return_value = {
            "approved": True,
            "approved_by": ["reviewer1"],
        }
        mock_gl.get_mr_notes.return_value = []

        result = wait_for_approval(
            gitlab=mock_gl,
            mr_iid=42,
            poll_interval_seconds=0,  # no sleeping in tests
            timeout_hours=0.001,
        )

        assert result.outcome == GateOutcome.APPROVED
        assert "reviewer1" in result.approved_by
        mock_gl.get_mr_approvals.assert_called_once_with(42)

    def test_approved_after_multiple_polls(self):
        """Approval arriving on the Nth poll should still succeed."""
        mock_gl = MagicMock()
        mock_gl.get_mr_approvals.side_effect = [
            {"approved": False, "approved_by": []},
            {"approved": False, "approved_by": []},
            {"approved": True, "approved_by": ["dev-a"]},
        ]
        mock_gl.get_mr_notes.return_value = []

        with patch("orchestrator.gate.time.sleep"):
            result = wait_for_approval(
                gitlab=mock_gl,
                mr_iid=10,
                poll_interval_seconds=0,
                timeout_hours=1,
            )

        assert result.outcome == GateOutcome.APPROVED
        assert mock_gl.get_mr_approvals.call_count == 3

    def test_timeout_raises(self):
        """Exceeding timeout_hours should raise GateTimeoutError."""
        mock_gl = MagicMock()
        mock_gl.get_mr_approvals.return_value = {"approved": False, "approved_by": []}
        mock_gl.get_mr_notes.return_value = []

        with patch("orchestrator.gate.time.sleep"):
            with patch("orchestrator.gate.time.monotonic") as mock_time:
                # First call: start time.  Second call: past deadline.
                mock_time.side_effect = [0.0, 0.0, 999999.0]
                with pytest.raises(GateTimeoutError):
                    wait_for_approval(
                        gitlab=mock_gl,
                        mr_iid=99,
                        poll_interval_seconds=0,
                        timeout_hours=0.001,
                    )

    def test_comments_returned_with_approval(self):
        """Reviewer comments should be included in the GateResult."""
        mock_gl = MagicMock()
        mock_gl.get_mr_approvals.return_value = {
            "approved": True,
            "approved_by": ["reviewer"],
        }
        mock_gl.get_mr_notes.return_value = [
            {"body": "Looks good, but add edge case", "system": False},
        ]

        result = wait_for_approval(
            gitlab=mock_gl,
            mr_iid=7,
            poll_interval_seconds=0,
            timeout_hours=1,
        )

        assert result.outcome == GateOutcome.APPROVED
        assert "Looks good, but add edge case" in result.reviewer_comments

    def test_seen_comments_prevents_immediate_retrigger(self):
        """Previously seen reviewer comments must not re-trigger CHANGES_REQUESTED."""
        mock_gl = MagicMock()
        mock_gl.get_mr_approvals.return_value = {"approved": False, "approved_by": []}
        mock_gl.get_mr_notes.return_value = [
            {"body": "Fix typo in button text", "system": False},
        ]

        seen = set()
        # First call sees the comment and returns CHANGES_REQUESTED
        result1 = wait_for_approval(
            gitlab=mock_gl,
            mr_iid=15,
            poll_interval_seconds=0,
            timeout_hours=1,
            return_on_changes_requested=True,
            seen_comments=seen,
        )
        assert result1.outcome == GateOutcome.CHANGES_REQUESTED
        assert "Fix typo in button text" in seen

        # Second call with the same seen set: it does not re-trigger on that comment
        # Instead, it continues waiting until approval
        mock_gl.get_mr_approvals.side_effect = [
            {"approved": False, "approved_by": []},
            {"approved": True, "approved_by": ["lead-dev"]},
        ]
        with patch("orchestrator.gate.time.sleep"):
            result2 = wait_for_approval(
                gitlab=mock_gl,
                mr_iid=15,
                poll_interval_seconds=0,
                timeout_hours=1,
                return_on_changes_requested=True,
                seen_comments=seen,
            )
        assert result2.outcome == GateOutcome.APPROVED
        assert "lead-dev" in result2.approved_by

    def test_bot_comments_are_ignored(self):
        """Comments from the Spec-Driven Dev bot should not count as reviewer comments."""
        mock_gl = MagicMock()
        mock_gl.get_mr_approvals.side_effect = [
            {"approved": False, "approved_by": []},
            {"approved": True, "approved_by": ["lead-dev"]},
        ]
        mock_gl.get_mr_notes.return_value = [
            {"body": "🤖 **Spec-Driven Dev Orchestrator**: Tests updated.", "system": False},
            {"body": "<!-- spec-driven-bot -->\nNotification", "system": False},
        ]

        with patch("orchestrator.gate.time.sleep"):
            result = wait_for_approval(
                gitlab=mock_gl,
                mr_iid=20,
                poll_interval_seconds=0,
                timeout_hours=1,
                return_on_changes_requested=True,
            )

        assert result.outcome == GateOutcome.APPROVED
        assert result.reviewer_comments == []

class TestGitHubWaitForApproval:
    def test_approved_on_first_poll(self):
        mock_gh = MagicMock()
        mock_gh.get_pr_approvals.return_value = {
            "approved": True,
            "approved_by": ["lead-reviewer"],
            "changes_requested": False,
        }
        mock_gh.get_pr_notes.return_value = []

        result = wait_for_approval(
            client=mock_gh,
            pr_number=101,
            poll_interval_seconds=0,
            timeout_hours=0.001,
        )

        assert result.outcome == GateOutcome.APPROVED
        assert "lead-reviewer" in result.approved_by
        mock_gh.get_pr_approvals.assert_called_once_with(101)

    def test_approved_after_multiple_polls(self):
        mock_gh = MagicMock()
        mock_gh.get_pr_approvals.side_effect = [
            {"approved": False, "approved_by": [], "changes_requested": False},
            {"approved": False, "approved_by": [], "changes_requested": False},
            {"approved": True, "approved_by": ["lead-reviewer"], "changes_requested": False},
        ]
        mock_gh.get_pr_notes.return_value = []

        with patch("orchestrator.gate.time.sleep"):
            result = wait_for_approval(
                client=mock_gh,
                pr_number=102,
                poll_interval_seconds=0,
                timeout_hours=1,
            )

        assert result.outcome == GateOutcome.APPROVED
        assert mock_gh.get_pr_approvals.call_count == 3

    def test_changes_requested_outcome(self):
        mock_gh = MagicMock()
        mock_gh.get_pr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": True,
        }
        mock_gh.get_pr_notes.return_value = [
            {"body": "Please revise the error handling logic", "system": False}
        ]

        result = wait_for_approval(
            client=mock_gh,
            pr_number=103,
            poll_interval_seconds=0,
            timeout_hours=1,
            return_on_changes_requested=True,
        )

        assert result.outcome == GateOutcome.CHANGES_REQUESTED
        assert "Please revise the error handling logic" in result.reviewer_comments

    def test_timeout_raises(self):
        mock_gh = MagicMock()
        mock_gh.get_pr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": False,
        }
        mock_gh.get_pr_notes.return_value = []

        with patch("orchestrator.gate.time.sleep"):
            with patch("orchestrator.gate.time.monotonic") as mock_time:
                mock_time.side_effect = [0.0, 0.0, 999999.0]
                with pytest.raises(GateTimeoutError):
                    wait_for_approval(
                        client=mock_gh,
                        pr_number=104,
                        poll_interval_seconds=0,
                        timeout_hours=0.001,
                    )

    def test_seen_comments_prevents_immediate_retrigger(self):
        """Previously seen comments on GitHub PR must not re-trigger CHANGES_REQUESTED."""
        mock_gh = MagicMock()
        mock_gh.get_pr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": False,
        }
        mock_gh.get_pr_notes.return_value = [
            {"body": "Fix CSS background", "system": False},
        ]

        seen = set()
        result1 = wait_for_approval(
            client=mock_gh,
            pr_number=105,
            poll_interval_seconds=0,
            timeout_hours=1,
            return_on_changes_requested=True,
            seen_comments=seen,
        )
        assert result1.outcome == GateOutcome.CHANGES_REQUESTED
        assert "Fix CSS background" in seen

        mock_gh.get_pr_approvals.side_effect = [
            {"approved": False, "approved_by": [], "changes_requested": False},
            {"approved": True, "approved_by": ["lead-dev"], "changes_requested": False},
        ]
        with patch("orchestrator.gate.time.sleep"):
            result2 = wait_for_approval(
                client=mock_gh,
                pr_number=105,
                poll_interval_seconds=0,
                timeout_hours=1,
                return_on_changes_requested=True,
                seen_comments=seen,
            )
        assert result2.outcome == GateOutcome.APPROVED

    def test_bot_comments_are_ignored(self):
        """Spec-Driven Dev bot comments must not count as reviewer comments."""
        mock_gh = MagicMock()
        mock_gh.get_pr_approvals.side_effect = [
            {"approved": False, "approved_by": [], "changes_requested": False},
            {"approved": True, "approved_by": ["lead-dev"], "changes_requested": False},
        ]
        mock_gh.get_pr_notes.return_value = [
            {"body": "🤖 **Spec-Driven Dev Orchestrator**: Revised tests.", "system": False},
            {"body": "<!-- spec-driven-bot -->\nNotification", "system": False},
        ]

        with patch("orchestrator.gate.time.sleep"):
            result = wait_for_approval(
                client=mock_gh,
                pr_number=106,
                poll_interval_seconds=0,
                timeout_hours=1,
                return_on_changes_requested=True,
            )

        assert result.outcome == GateOutcome.APPROVED
        assert result.reviewer_comments == []

    def test_approval_via_comment_keyword(self):
        """Conversational approval keywords like 'Approved', '/approve', 'lgtm' must approve Gate."""
        mock_gh = MagicMock()
        mock_gh.get_pr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": False,
        }
        mock_gh.get_pr_notes.return_value = [
            {"body": "Approved", "system": False},
        ]

        result = wait_for_approval(
            client=mock_gh,
            pr_number=107,
            poll_interval_seconds=0,
            timeout_hours=1,
            return_on_changes_requested=True,
        )

        assert result.outcome == GateOutcome.APPROVED
        assert "reviewer-comment" in result.approved_by
