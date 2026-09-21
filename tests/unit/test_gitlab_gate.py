"""
Unit tests for orchestrator.gitlab_gate.
"""

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.gitlab_gate import (
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

        with patch("orchestrator.gitlab_gate.time.sleep"):
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

        with patch("orchestrator.gitlab_gate.time.sleep"):
            with patch("orchestrator.gitlab_gate.time.monotonic") as mock_time:
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
