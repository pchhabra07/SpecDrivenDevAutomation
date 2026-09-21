"""
Unit tests for orchestrator.config — validates that Settings loads
correctly from environment variables.
"""

import os
from unittest.mock import patch

import pytest

from orchestrator.config import Settings


# Minimum set of env vars required to instantiate Settings without error.
_REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://test.atlassian.net",
    "JIRA_API_TOKEN": "jira-token",
    "JIRA_USER_EMAIL": "user@example.com",
    "JIRA_PROJECT_KEY": "TEST",
    "GITLAB_BASE_URL": "https://gitlab.example.com",
    "GITLAB_API_TOKEN": "gl-token",
    "GITLAB_REPO_ID": "42",
    "NIM_API_KEY": "nim-key",
    "NIM_BASE_URL": "https://integrate.api.nvidia.com/v1",
}


class TestSettings:
    """Tests for the centralised Settings model."""

    def test_loads_required_vars(self):
        """Settings should instantiate when all required vars are present."""
        with patch.dict(os.environ, _REQUIRED_ENV, clear=False):
            s = Settings()
            assert s.jira_base_url == "https://test.atlassian.net"
            assert s.gitlab_repo_id == 42
            assert s.nim_api_key == "nim-key"

    def test_defaults_are_applied(self):
        """Optional fields should use their defaults."""
        with patch.dict(os.environ, _REQUIRED_ENV, clear=False):
            s = Settings()
            assert s.qualifying_label == "SpecDrivenDev"
            assert s.nim_model == "z-ai/glm-5.3"
            assert s.rate_limit_cap == 32
            assert s.max_code_iterations == 6
            assert s.coverage_threshold == 80
            assert s.gate_poll_interval_seconds == 30

    def test_missing_required_raises(self):
        """Omitting a required field should raise a validation error."""
        incomplete = dict(_REQUIRED_ENV)
        del incomplete["JIRA_API_TOKEN"]
        with patch.dict(os.environ, incomplete, clear=True):
            with pytest.raises(Exception):
                Settings(_env_file=None)

    def test_env_override(self):
        """Explicit env vars should override defaults."""
        overrides = {
            **_REQUIRED_ENV,
            "QUALIFYING_LABEL": "CustomLabel",
            "RATE_LIMIT_CAP": "10",
            "MAX_CODE_ITERATIONS": "3",
        }
        with patch.dict(os.environ, overrides, clear=False):
            s = Settings()
            assert s.qualifying_label == "CustomLabel"
            assert s.rate_limit_cap == 10
            assert s.max_code_iterations == 3
