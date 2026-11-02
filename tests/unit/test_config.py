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
    "GITHUB_TOKEN": "ghp_1234567890abcdef",
    "GITHUB_REPO": "pchhabra07/SpecDrivenDevAutomation",
    "NIM_API_KEY": "nim-key",
    "NIM_BASE_URL": "https://integrate.api.nvidia.com/v1",
}


class TestSettings:
    """Tests for the centralised Settings model."""

    def test_loads_required_vars(self):
        """Settings should instantiate when all required vars are present."""
        with patch.dict(os.environ, _REQUIRED_ENV, clear=False):
            s = Settings()
            assert s.github_token == "ghp_1234567890abcdef"
            assert s.github_repo == "pchhabra07/SpecDrivenDevAutomation"
            assert s.nim_api_key == "nim-key"

    def test_defaults_are_applied(self):
        """Optional fields should use their defaults."""
        with patch.dict(os.environ, _REQUIRED_ENV, clear=False):
            s = Settings(_env_file=None)
            assert s.qualifying_label == "SpecDrivenDev"
            assert s.nim_model == "z-ai/glm-5.3"
            assert s.rate_limit_cap == 32
            assert s.max_code_iterations == 6
            assert s.coverage_threshold == 80
            assert s.gate_poll_interval_seconds == 30

    def test_missing_required_raises(self):
        """Omitting a required field should raise a validation error."""
        incomplete = dict(_REQUIRED_ENV)
        del incomplete["GITHUB_TOKEN"]
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

    def test_repo_normalization(self):
        """Full GitHub URLs should be normalized to owner/repo format."""
        github_env = {
            **_REQUIRED_ENV,
            "GITHUB_REPO": "https://github.com/pchhabra07/SpecDrivenDevAutomation.git",
            "GITHUB_AUDIT_REPO": "git@github.com:pchhabra07/audit-log.git",
        }
        with patch.dict(os.environ, github_env, clear=True):
            s = Settings(_env_file=None)
            assert s.github_repo == "pchhabra07/SpecDrivenDevAutomation"
            assert s.github_audit_repo == "pchhabra07/audit-log"
