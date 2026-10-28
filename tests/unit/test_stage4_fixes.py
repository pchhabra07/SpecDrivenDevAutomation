"""
Unit tests covering Stage 4 fixes for problems S4-1 through S4-3:
- S4-1: Stage 4 gate failure auto-repair loop with pre-flight check and tamper protection.
- S4-2: Gate status 'skipped' vs 'unknown' resolution policy; non-blocking absent gates in passing CI.
- S4-3: Reuse Stage 3 final full CI pipeline result; single-use clearing; fallback when disabled.
- Client log methods: get_job_log on GitLabClient and GitHubClient.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.ci_poller import CIResult
from orchestrator.config import Settings
from orchestrator.github_client import GitHubClient, WorkflowRunResult
from orchestrator.models import PipelineResult, TicketData
from orchestrator.main import _attempt_stage4_repair, run_pipeline
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.stages.stage4_validation import (
    GateStatus,
    ValidationResult,
    _map_gate_statuses,
    _resolve_missing_gate_status,
    run as stage4_run,
)


def _make_context(
    source_files: dict[str, str] | None = None,
    test_files: dict[str, str] | None = None,
) -> TicketContext:
    return TicketContext(
        ticket=TicketData(
            ticket_id="TEST-401",
            summary="Validate Stage 4 fixes",
            description="Fix linting and CI gate issues.",
            acceptance_criteria="- Pass all quality gates",
            labels=["SpecDrivenDev"],
        ),
        default_branch="main",
        source_files=source_files or {"src/app.py": "x = 1\n"},
        test_files=test_files or {"tests/test_app.py": "def test_ok(): pass\n"},
        all_file_paths=list((source_files or {"src/app.py": ""}).keys()),
    )


# ── S4-2: Gate Status Resolution Policy ─────────────────────────────────


class TestS4_2_GateResolutionPolicy:
    def test_resolve_missing_gate_when_ci_passed(self):
        status, required = _resolve_missing_gate_status("lint", ci_passed=True, sonar_configured=True)
        assert status == "skipped"
        assert required is False

    def test_resolve_missing_gate_when_ci_failed(self):
        status, required = _resolve_missing_gate_status("lint", ci_passed=False, sonar_configured=True)
        assert status == "unknown"
        assert required is False

    def test_resolve_quality_gate_when_sonar_not_configured(self):
        status, required = _resolve_missing_gate_status("quality-gate", ci_passed=True, sonar_configured=False)
        assert status == "skipped"
        assert required is False

        status2, required2 = _resolve_missing_gate_status("quality-gate", ci_passed=False, sonar_configured=False)
        assert status2 == "skipped"
        assert required2 is False

    def test_passing_ci_with_unmapped_jobs_evaluates_overall_passed(self):
        # Overall CI passed, but repo only defines a single 'test' job
        ci = CIResult(
            pipeline_id=101,
            status="success",
            web_url="https://ci.example.com/101",
            jobs=[{"name": "test", "status": "success"}],
        )
        gates = _map_gate_statuses(ci, sonar_configured=False)
        # Required gates must not block if the pipeline succeeded and jobs weren't defined
        required_gates = [g for g in gates if g.required]
        # test-coverage matches 'test' alias so it's present and required
        assert all(g.passed for g in required_gates)
        # Absent gates (lint, sast, dependency-scan) are marked skipped and not required
        skipped_gates = [g for g in gates if g.status == "skipped"]
        assert len(skipped_gates) >= 3


# ── S4-3: Reuse Stage 3 Final Pipeline Result ───────────────────────────


class TestS4_3_ReuseStage3Pipeline:
    def test_stage4_reuses_cached_pipeline_result(self):
        cp = Checkpoint(ticket_id="TEST-402", code_branch="automation/TEST-402/code")
        cp.stage3_final_pipeline_result = {
            "pipeline_id": 999,
            "status": "success",
            "web_url": "https://ci.example.com/999",
            "jobs": [
                {"name": "lint", "status": "success"},
                {"name": "sast", "status": "success"},
                {"name": "dependency-scan", "status": "success"},
                {"name": "test-coverage", "status": "success"},
            ],
        }

        mock_gitlab = MagicMock()
        mock_audit = MagicMock()

        result = stage4_run(
            code_branch=cp.code_branch,
            gitlab=mock_gitlab,
            checkpoint=cp,
            audit=mock_audit,
            reuse_stage3_pipeline=True,
            sonar_configured=False,
        )

        assert result.passed is True
        assert result.pipeline_id == 999
        # trigger_ci should NOT have been called because result was reused
        mock_gitlab.trigger_pipeline.assert_not_called()
        # Cache must be cleared after single use to prevent stale reuse
        assert cp.stage3_final_pipeline_result == {}

    def test_stage4_triggers_fresh_ci_when_reuse_disabled(self):
        cp = Checkpoint(ticket_id="TEST-403", code_branch="automation/TEST-403/code")
        cp.stage3_final_pipeline_result = {
            "pipeline_id": 888,
            "status": "success",
            "web_url": "https://ci.example.com/888",
            "jobs": [],
        }

        mock_gitlab = MagicMock()
        mock_gitlab.trigger_pipeline.return_value = 1001
        mock_gitlab.get_pipeline_status.return_value = PipelineResult(
            pipeline_id=1001,
            status="success",
            web_url="https://ci.example.com/1001",
            jobs=[{"name": "lint", "status": "success"}],
        )
        mock_audit = MagicMock()

        result = stage4_run(
            code_branch=cp.code_branch,
            gitlab=mock_gitlab,
            checkpoint=cp,
            audit=mock_audit,
            reuse_stage3_pipeline=False,
            ci_poll_interval=1,
            ci_timeout_minutes=1,
        )

        assert result.pipeline_id == 1001
        mock_gitlab.trigger_pipeline.assert_called_once()


# ── S4-1: Stage 4 Gate Failure LLM Auto-Repair Loop ─────────────────────


class TestS4_1_Stage4RepairLoop:
    def test_attempt_stage4_repair_successful(self):
        context = _make_context(source_files={"src/app.py": "import os\nx = 1\n"})
        checkpoint = Checkpoint(
            ticket_id="TEST-404",
            code_branch="automation/TEST-404/code",
            locked_test_hashes={"tests/locked.py": "hash123"},
        )
        mock_gitlab = MagicMock()
        mock_gitlab.get_file_content.return_value = "import os\nx = 1\n"
        mock_audit = MagicMock()

        # LLM returns a valid SEARCH/REPLACE block removing unused import
        repair_patch = (
            "// FILE: src/app.py\n"
            "<<<<<<< SEARCH\n"
            "import os\n"
            "x = 1\n"
            "=======\n"
            "x = 1\n"
            ">>>>>>> REPLACE"
        )
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"content": repair_patch}

        gate_logs = {"lint": "src/app.py:1:1: F401 'os' imported but unused"}

        repaired = _attempt_stage4_repair(
            context=context,
            checkpoint=checkpoint,
            gitlab=mock_gitlab,
            llm=mock_llm,
            audit=mock_audit,
            gate_logs=gate_logs,
        )

        assert repaired is True
        mock_gitlab.commit_files.assert_called_once()
        committed_files = mock_gitlab.commit_files.call_args[1]["files"]
        assert "src/app.py" in committed_files
        assert "import os" not in committed_files["src/app.py"]

    def test_attempt_stage4_repair_rejects_locked_file_tampering(self):
        context = _make_context()
        checkpoint = Checkpoint(
            ticket_id="TEST-405",
            code_branch="automation/TEST-405/code",
            locked_test_hashes={"tests/locked.py": "hash123"},
        )
        mock_gitlab = MagicMock()
        mock_audit = MagicMock()

        # LLM tries to tamper with locked test file
        repair_patch = (
            "// FILE: tests/locked.py\n"
            "<<<<<<< SEARCH\n"
            "def test_old(): pass\n"
            "=======\n"
            "def test_new(): pass\n"
            ">>>>>>> REPLACE"
        )
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"content": repair_patch}

        repaired = _attempt_stage4_repair(
            context=context,
            checkpoint=checkpoint,
            gitlab=mock_gitlab,
            llm=mock_llm,
            audit=mock_audit,
            gate_logs={"lint": "error in tests/locked.py"},
        )

        # Must reject without committing
        assert repaired is False
        mock_gitlab.commit_files.assert_not_called()

    def test_attempt_stage4_repair_rejects_syntax_errors(self):
        context = _make_context(source_files={"src/app.py": "x = 1\n"})
        checkpoint = Checkpoint(ticket_id="TEST-406", code_branch="automation/TEST-406/code")
        mock_gitlab = MagicMock()
        mock_gitlab.get_file_content.return_value = "x = 1\n"
        mock_audit = MagicMock()

        # LLM introduces a Python syntax error
        repair_patch = (
            "// FILE: src/app.py\n"
            "<<<<<<< SEARCH\n"
            "x = 1\n"
            "=======\n"
            "def invalid_syntax(:\n"
            ">>>>>>> REPLACE"
        )
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {"content": repair_patch}

        repaired = _attempt_stage4_repair(
            context=context,
            checkpoint=checkpoint,
            gitlab=mock_gitlab,
            llm=mock_llm,
            audit=mock_audit,
            gate_logs={"lint": "style error"},
        )

        # Pre-flight check must catch syntax error and reject commit
        assert repaired is False
        mock_gitlab.commit_files.assert_not_called()


# ── Client Log Retrieval Methods ────────────────────────────────────────


class TestClientLogMethods:
    def test_github_client_get_job_log(self):
        client = GitHubClient.__new__(GitHubClient)
        client._repo_name = "test/repo"
        client._token = "mock-token"
        mock_repo = MagicMock()
        client._repo = mock_repo

        mock_job = MagicMock()
        mock_job.id = 55
        mock_job.name = "lint"

        mock_run = MagicMock()
        mock_run.jobs.return_value = [mock_job]
        mock_repo.get_workflow_run.return_value = mock_run

        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "GitHub Actions Lint Log"
            mock_get.return_value = mock_resp

            log = client.get_job_log(100, "lint")
            assert "GitHub Actions Lint Log" in log
