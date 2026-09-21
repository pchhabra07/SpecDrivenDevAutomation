"""Unit tests for orchestrator.ci_poller."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.ci_poller import (
    CIResult,
    CITimeoutError,
    trigger_ci,
    wait_for_pipeline,
)
from orchestrator.gitlab_client import PipelineResult


class TestTriggerCI:
    """Tests for the ``trigger_ci`` function."""

    def test_delegates_to_gitlab_client(self):
        """Should call gitlab.trigger_pipeline and return the pipeline ID."""
        gitlab = MagicMock()
        gitlab.trigger_pipeline.return_value = 42

        result = trigger_ci(gitlab, "feature-branch", variables={"FOO": "bar"})

        assert result == 42
        gitlab.trigger_pipeline.assert_called_once_with(
            "feature-branch", variables={"FOO": "bar"},
        )

    def test_no_variables(self):
        """Should work without CI variables."""
        gitlab = MagicMock()
        gitlab.trigger_pipeline.return_value = 99

        result = trigger_ci(gitlab, "main")

        assert result == 99
        gitlab.trigger_pipeline.assert_called_once_with("main", variables=None)


class TestWaitForPipeline:
    """Tests for the ``wait_for_pipeline`` polling loop."""

    def test_returns_immediately_on_success(self):
        """Should return CIResult when pipeline succeeds on first poll."""
        gitlab = MagicMock()
        gitlab.get_pipeline_status.return_value = PipelineResult(
            pipeline_id=1,
            status="success",
            web_url="https://gitlab.com/pipelines/1",
            jobs=[{"name": "test", "stage": "test", "status": "success"}],
        )

        result = wait_for_pipeline(gitlab, 1, poll_interval_seconds=1, timeout_minutes=1)

        assert isinstance(result, CIResult)
        assert result.passed is True
        assert result.status == "success"
        assert result.pipeline_id == 1

    def test_returns_on_failure(self):
        """Should return CIResult when pipeline fails."""
        gitlab = MagicMock()
        gitlab.get_pipeline_status.return_value = PipelineResult(
            pipeline_id=2,
            status="failed",
            web_url="https://gitlab.com/pipelines/2",
            jobs=[{"name": "test", "stage": "test", "status": "failed"}],
        )

        result = wait_for_pipeline(gitlab, 2, poll_interval_seconds=1, timeout_minutes=1)

        assert result.passed is False
        assert result.status == "failed"

    @patch("orchestrator.ci_poller.time.sleep")
    def test_polls_until_terminal(self, mock_sleep):
        """Should poll multiple times until a terminal state is reached."""
        gitlab = MagicMock()
        # First poll: running, second poll: success.
        gitlab.get_pipeline_status.side_effect = [
            PipelineResult(pipeline_id=3, status="running", web_url="", jobs=[]),
            PipelineResult(
                pipeline_id=3, status="success", web_url="https://gitlab.com/pipelines/3",
                jobs=[{"name": "test", "stage": "test", "status": "success"}],
            ),
        ]

        result = wait_for_pipeline(gitlab, 3, poll_interval_seconds=1, timeout_minutes=5)

        assert result.passed is True
        assert gitlab.get_pipeline_status.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch("orchestrator.ci_poller.time.sleep")
    @patch("orchestrator.ci_poller.time.monotonic")
    def test_raises_timeout(self, mock_monotonic, mock_sleep):
        """Should raise CITimeoutError when timeout is exceeded."""
        # Simulate time advancing past the deadline.
        mock_monotonic.side_effect = [0, 0, 1900]  # start, first poll, past deadline

        gitlab = MagicMock()
        gitlab.get_pipeline_status.return_value = PipelineResult(
            pipeline_id=4, status="running", web_url="", jobs=[],
        )

        with pytest.raises(CITimeoutError, match="did not complete within"):
            wait_for_pipeline(gitlab, 4, poll_interval_seconds=1, timeout_minutes=1)

    def test_returns_on_canceled(self):
        """Should return CIResult when pipeline is canceled."""
        gitlab = MagicMock()
        gitlab.get_pipeline_status.return_value = PipelineResult(
            pipeline_id=5, status="canceled", web_url="https://gitlab.com/pipelines/5",
            jobs=[],
        )

        result = wait_for_pipeline(gitlab, 5, poll_interval_seconds=1, timeout_minutes=1)

        assert result.status == "canceled"
        assert result.passed is False


class TestCIResult:
    """Tests for the ``CIResult`` dataclass."""

    def test_job_status_found(self):
        result = CIResult(
            pipeline_id=1, status="success", web_url="",
            jobs=[
                {"name": "test", "status": "success"},
                {"name": "lint", "status": "failed"},
            ],
        )
        assert result.job_status("test") == "success"
        assert result.job_status("lint") == "failed"

    def test_job_status_not_found(self):
        result = CIResult(pipeline_id=1, status="success", web_url="", jobs=[])
        assert result.job_status("nonexistent") is None
