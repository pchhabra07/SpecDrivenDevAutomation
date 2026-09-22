"""
Unit tests for orchestrator.github_gate.
"""

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.github_gate import (
    GateOutcome,
    GateResult,
    GateTimeoutError,
    wait_for_approval,
)


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

        with patch("orchestrator.github_gate.time.sleep"):
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

        with patch("orchestrator.github_gate.time.sleep"):
            with patch("orchestrator.github_gate.time.monotonic") as mock_time:
                mock_time.side_effect = [0.0, 0.0, 999999.0]
                with pytest.raises(GateTimeoutError):
                    wait_for_approval(
                        client=mock_gh,
                        pr_number=104,
                        poll_interval_seconds=0,
                        timeout_hours=0.001,
                    )
