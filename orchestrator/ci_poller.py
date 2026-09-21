"""
CI/CD pipeline trigger and polling — thin coordination layer.

Triggers a GitLab CI/CD pipeline for a given branch and polls
``gitlab_client.get_pipeline_status`` until the pipeline reaches a
terminal state (``success``, ``failed``, ``canceled``).

Used by Stage 3 (test-only runs) and Stage 4 (full validation).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from orchestrator.gitlab_client import GitLabClient, PipelineResult

logger = logging.getLogger(__name__)

_TERMINAL_STATES = {"success", "failed", "canceled"}


class CITimeoutError(Exception):
    """Raised when a CI pipeline does not finish within *timeout_minutes*."""


@dataclass
class CIResult:
    """Structured result of a CI pipeline run."""

    pipeline_id: int
    status: str  # "success", "failed", "canceled"
    web_url: str
    jobs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "success"

    def job_status(self, job_name: str) -> str | None:
        """Return the status of a specific job by name, or None if not found."""
        for job in self.jobs:
            if job.get("name") == job_name:
                return job.get("status")
        return None


def trigger_ci(
    gitlab: GitLabClient,
    branch: str,
    variables: dict[str, str] | None = None,
) -> int:
    """
    Trigger a CI/CD pipeline on *branch* and return its pipeline ID.

    Parameters
    ----------
    gitlab:
        Initialised ``GitLabClient``.
    branch:
        The Git branch to run the pipeline on.
    variables:
        Optional CI/CD variables to pass (e.g. ``TEST_ONLY=true``).

    Returns
    -------
    int
        The pipeline ID.
    """
    pipeline_id = gitlab.trigger_pipeline(branch, variables=variables)
    logger.info("CI pipeline %d triggered on branch %s", pipeline_id, branch)
    return pipeline_id


def wait_for_pipeline(
    gitlab: GitLabClient,
    pipeline_id: int,
    poll_interval_seconds: int = 30,
    timeout_minutes: int = 30,
) -> CIResult:
    """
    Poll a pipeline until it reaches a terminal state.

    Parameters
    ----------
    gitlab:
        Initialised ``GitLabClient``.
    pipeline_id:
        The pipeline ID to monitor.
    poll_interval_seconds:
        Seconds between status polls.
    timeout_minutes:
        Maximum minutes to wait before raising ``CITimeoutError``.

    Returns
    -------
    CIResult
        Structured result with pipeline status and per-job breakdown.

    Raises
    ------
    CITimeoutError
        If the pipeline does not complete within *timeout_minutes*.
    """
    deadline = time.monotonic() + (timeout_minutes * 60)
    logger.info(
        "Waiting for pipeline %d (timeout=%dm, poll=%ds)",
        pipeline_id, timeout_minutes, poll_interval_seconds,
    )

    while time.monotonic() < deadline:
        result: PipelineResult = gitlab.get_pipeline_status(pipeline_id)

        if result.status in _TERMINAL_STATES:
            ci_result = CIResult(
                pipeline_id=result.pipeline_id,
                status=result.status,
                web_url=result.web_url,
                jobs=result.jobs,
            )
            logger.info(
                "Pipeline %d finished: %s (%s)",
                pipeline_id, result.status, result.web_url,
            )
            return ci_result

        logger.debug(
            "Pipeline %d status: %s — polling again in %ds",
            pipeline_id, result.status, poll_interval_seconds,
        )
        time.sleep(poll_interval_seconds)

    raise CITimeoutError(
        f"Pipeline {pipeline_id} did not complete within {timeout_minutes} minutes"
    )
