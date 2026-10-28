"""Unit tests for orchestrator.stages.stage3_code_gen."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.ci_poller import CIResult
from orchestrator.models import MergeRequestInfo, TicketData
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.stages.stage3_code_gen import (
    _generate_code,
    _parse_generated_files,
    _parse_ci_test_results,
    _fetch_locked_test_contents,
    run,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_context() -> TicketContext:
    """Create a minimal TicketContext for testing."""
    return TicketContext(
        ticket=TicketData(
            ticket_id="PROJ-42",
            summary="Fix widget rendering",
            description="The widget breaks on mobile.",
            acceptance_criteria="- Widget renders on mobile\n- No JS errors",
            labels=["SpecDrivenDev"],
        ),
        default_branch="main",
        source_files={"src/widget.ts": "export const Widget = () => {};"},
        test_files={"src/__tests__/widget.test.ts": "test('renders', () => {});"},
        all_file_paths=["src/widget.ts", "src/__tests__/widget.test.ts"],
    )


def _make_checkpoint() -> Checkpoint:
    """Create a checkpoint at Stage 3 with locked test hashes."""
    cp = Checkpoint(ticket_id="PROJ-42")
    cp.current_stage = 3
    cp.locked_test_hashes = {
        "src/__tests__/widget.test.ts": "abc123hash",
    }
    return cp


# ── Parser Tests ────────────────────────────────────────────────────────


class TestParseGeneratedFiles:
    """Tests for the ``_parse_generated_files`` helper."""

    def test_parses_single_file(self):
        output = (
            "// FILE: src/widget.ts\n"
            "```typescript\n"
            "export const Widget = () => <div />;\n"
            "```\n"
        )
        result = _parse_generated_files(output)
        assert "src/widget.ts" in result
        assert "Widget" in result["src/widget.ts"]

    def test_parses_multiple_files(self):
        output = (
            "// FILE: src/a.ts\n"
            "```typescript\nconst a = 1;\n```\n"
            "// FILE: src/b.ts\n"
            "```typescript\nconst b = 2;\n```\n"
        )
        result = _parse_generated_files(output)
        assert len(result) == 2
        assert "src/a.ts" in result
        assert "src/b.ts" in result

    def test_empty_output(self):
        assert _parse_generated_files("") == {}

    def test_no_file_markers(self):
        assert _parse_generated_files("some random text") == {}


# ── CI Result Parsing ───────────────────────────────────────────────────


class TestParseCITestResults:
    """Tests for ``_parse_ci_test_results``."""

    def test_success(self):
        ci = CIResult(
            pipeline_id=1, status="success", web_url="",
            jobs=[{"name": "test", "status": "success"}],
        )
        result = _parse_ci_test_results(ci)
        assert result["failed"] == 0
        assert result["test_job_status"] == "success"

    def test_failure(self):
        ci = CIResult(
            pipeline_id=2, status="failed", web_url="",
            jobs=[{"name": "test", "status": "failed"}],
        )
        result = _parse_ci_test_results(ci)
        assert result["failed"] == 1
        assert result["test_job_status"] == "failed"

    def test_missing_test_job(self):
        ci = CIResult(pipeline_id=3, status="success", web_url="", jobs=[])
        result = _parse_ci_test_results(ci)
        assert result["test_job_status"] == "unknown"


# ── Locked File Fetch ───────────────────────────────────────────────────


class TestFetchLockedTestContents:
    """Tests for ``_fetch_locked_test_contents``."""

    def test_fetches_all_locked_files(self):
        gitlab = MagicMock()
        gitlab.get_file_content.return_value = "test content"

        result = _fetch_locked_test_contents(
            gitlab, "feature-branch", {"a.test.ts", "b.test.ts"},
        )

        assert len(result) == 2
        assert gitlab.get_file_content.call_count == 2

    def test_handles_missing_file(self):
        gitlab = MagicMock()
        gitlab.get_file_content.side_effect = Exception("not found")

        result = _fetch_locked_test_contents(
            gitlab, "feature-branch", {"missing.test.ts"},
        )

        assert result == {}


# ── Code Generation with Locked File Rejection ─────────────────────────


class TestGenerateCode:
    """Tests for ``_generate_code`` with locked file handling."""

    def test_rejects_locked_file_modifications(self):
        """LLM tries to modify a locked file → should be removed from result."""
        llm = MagicMock()
        # First response includes a locked file, second is clean.
        llm.chat.side_effect = [
            {"content": (
                "// FILE: src/__tests__/widget.test.ts\n"
                "```typescript\nmodified test\n```\n"
                "// FILE: src/widget.ts\n"
                "```typescript\nfixed code\n```\n"
            )},
            {"content": (
                "// FILE: src/widget.ts\n"
                "```typescript\nfixed code v2\n```\n"
            )},
        ]

        context = _make_context()
        result = _generate_code(
            llm=llm,
            context=context,
            current_sources=context.source_files,
            last_test_output="",
            iteration=1,
            locked_paths={"src/__tests__/widget.test.ts"},
        )

        # Should have re-prompted after first attempt.
        assert llm.chat.call_count == 2
        assert "src/__tests__/widget.test.ts" not in result

    def test_returns_clean_files(self):
        """LLM returns only source files → no re-prompting needed."""
        llm = MagicMock()
        llm.chat.return_value = {
            "content": (
                "// FILE: src/widget.ts\n"
                "```typescript\nfixed code\n```\n"
            ),
        }

        context = _make_context()
        result = _generate_code(
            llm=llm,
            context=context,
            current_sources=context.source_files,
            last_test_output="",
            iteration=1,
            locked_paths={"src/__tests__/widget.test.ts"},
        )

        assert llm.chat.call_count == 1
        assert "src/widget.ts" in result


# ── Full Run Integration ────────────────────────────────────────────────


class TestStage3Run:
    """Integration-level tests for the Stage 3 ``run`` function."""

    @patch("orchestrator.stages.stage3_code_gen.wait_for_pipeline")
    @patch("orchestrator.stages.stage3_code_gen.trigger_ci")
    def test_successful_run_on_second_iteration(self, mock_trigger, mock_wait):
        """Tests pass on iteration 2 → should exit loop and set stage=4."""
        gitlab = MagicMock()
        gitlab.branch_exists.return_value = False
        gitlab.create_branch.return_value = None
        gitlab.create_mr.return_value = MergeRequestInfo(
            iid=10, web_url="https://gitlab.com/mr/10", state="opened", draft=True,
        )
        gitlab.commit_files.return_value = "sha123"
        gitlab.get_file_content.return_value = "test('renders', () => {});"

        llm = MagicMock()
        # Iteration 1: returns code, iteration 2: returns better code,
        # blocker assessment not called since tests pass.
        llm.chat.side_effect = [
            {"content": "// FILE: src/widget.ts\n```typescript\nv1\n```\n"},
            {"content": "// FILE: src/widget.ts\n```typescript\nv2\n```\n"},
        ]

        jira = MagicMock()
        audit = MagicMock(spec=AuditLogger)
        checkpoint = _make_checkpoint()
        # Use a real hash matching the content returned by gitlab.get_file_content.
        from orchestrator.tamper_check import hash_content
        checkpoint.locked_test_hashes = {
            "src/__tests__/widget.test.ts": hash_content("test('renders', () => {});"),
        }

        mock_trigger.return_value = 100
        # Iteration 1: test fails, iteration 2: test passes.
        mock_wait.side_effect = [
            CIResult(
                pipeline_id=100, status="failed", web_url="",
                jobs=[{"name": "test", "status": "failed"}],
            ),
            CIResult(
                pipeline_id=101, status="success", web_url="",
                jobs=[{"name": "test", "status": "success"}],
            ),
        ]

        result = run(
            context=_make_context(),
            gitlab=gitlab,
            llm=llm,
            jira=jira,
            checkpoint=checkpoint,
            audit=audit,
            max_iterations=6,
            ci_poll_interval=1,
            ci_timeout_minutes=1,
        )

        assert result["failed"] == 0
        assert checkpoint.current_stage == 4
        assert checkpoint.iteration_count == 2

    @patch("orchestrator.stages.stage3_code_gen.wait_for_pipeline")
    @patch("orchestrator.stages.stage3_code_gen.trigger_ci")
    @patch("orchestrator.stages.stage3_code_gen.notify_slack")
    @patch("orchestrator.stages.stage3_code_gen.notify_email")
    def test_tamper_detected_halts_loop(
        self, mock_email, mock_slack, mock_trigger, mock_wait,
    ):
        """Tamper detection should raise RuntimeError and trigger escalation."""
        gitlab = MagicMock()
        gitlab.branch_exists.return_value = True
        gitlab.create_mr.return_value = MergeRequestInfo(
            iid=11, web_url="", state="opened", draft=True,
        )
        gitlab.commit_files.return_value = "sha456"
        # Return DIFFERENT content from what was locked → tamper.
        gitlab.get_file_content.return_value = "TAMPERED CONTENT"

        llm = MagicMock()
        llm.chat.return_value = {
            "content": "// FILE: src/widget.ts\n```typescript\ncode\n```\n",
        }

        jira = MagicMock()
        audit = MagicMock(spec=AuditLogger)
        checkpoint = _make_checkpoint()
        # Use a real hash that won't match "TAMPERED CONTENT".
        checkpoint.locked_test_hashes = {
            "src/__tests__/widget.test.ts": "0000000000000000000000000000000000000000000000000000000000000000",
        }

        mock_trigger.return_value = 200
        mock_wait.return_value = CIResult(
            pipeline_id=200, status="success", web_url="",
            jobs=[{"name": "test", "status": "success"}],
        )

        with pytest.raises(RuntimeError, match="Tamper detected"):
            run(
                context=_make_context(),
                gitlab=gitlab,
                llm=llm,
                jira=jira,
                checkpoint=checkpoint,
                audit=audit,
                max_iterations=6,
                ci_poll_interval=1,
                ci_timeout_minutes=1,
            )

        # Escalation should have been triggered.
        gitlab.add_mr_label.assert_called_once()
        jira.post_comment.assert_called_once()

    @patch("orchestrator.stages.stage3_code_gen.wait_for_pipeline")
    @patch("orchestrator.stages.stage3_code_gen.trigger_ci")
    @patch("orchestrator.stages.stage3_code_gen.notify_slack")
    @patch("orchestrator.stages.stage3_code_gen.notify_email")
    def test_max_iterations_triggers_escalation(
        self, mock_email, mock_slack, mock_trigger, mock_wait,
    ):
        """Exhausting max iterations should escalate."""
        gitlab = MagicMock()
        gitlab.branch_exists.return_value = True
        gitlab.create_mr.return_value = MergeRequestInfo(
            iid=12, web_url="", state="opened", draft=True,
        )
        gitlab.commit_files.return_value = "sha789"
        # No tampering — return matching content.
        gitlab.get_file_content.return_value = "test('renders', () => {});"

        llm = MagicMock()
        llm.chat.return_value = {
            "content": "// FILE: src/widget.ts\n```typescript\ncode\n```\n",
        }

        jira = MagicMock()
        audit = MagicMock(spec=AuditLogger)
        checkpoint = _make_checkpoint()
        # Use a hash that matches the content returned by gitlab.get_file_content.
        from orchestrator.tamper_check import hash_content
        checkpoint.locked_test_hashes = {
            "src/__tests__/widget.test.ts": hash_content("test('renders', () => {});"),
        }

        mock_trigger.return_value = 300
        # All iterations fail.
        mock_wait.return_value = CIResult(
            pipeline_id=300, status="failed", web_url="",
            jobs=[{"name": "test", "status": "failed"}],
        )

        with pytest.raises(RuntimeError, match="exhausted 2 iterations"):
            run(
                context=_make_context(),
                gitlab=gitlab,
                llm=llm,
                jira=jira,
                checkpoint=checkpoint,
                audit=audit,
                max_iterations=2,  # Small number for fast test.
                ci_poll_interval=1,
                ci_timeout_minutes=1,
            )

        # Escalation calls.
        gitlab.add_mr_label.assert_called_once()
        jira.post_comment.assert_called_once()


# ── N1 Fix: History cleared before each iteration > 1 ─────────────────


class TestN1HistoryClear:
    """N1 fix: LLM history must be reset before every iteration after the first.

    Without this, the model uses its in-context memory of the original file
    content to write SEARCH blocks — even though prior iterations already
    patched those exact lines.  The result is cascading PATCH NOT FOUND errors.
    """

    @patch("orchestrator.stages.stage3_code_gen.wait_for_pipeline")
    @patch("orchestrator.stages.stage3_code_gen.trigger_ci")
    def test_clear_history_called_on_iteration_2_and_beyond(
        self, mock_trigger, mock_wait,
    ):
        """clear_history(keep_system=True) must be called at iteration 2+."""
        gitlab = MagicMock()
        gitlab.branch_exists.return_value = True
        gitlab.create_mr.return_value = MergeRequestInfo(
            iid=99, web_url="", state="opened", draft=True,
        )
        gitlab.commit_files.return_value = "sha_n1"
        gitlab.get_file_content.return_value = "test('renders', () => {});"

        llm = MagicMock()
        # Iteration 1: returns code change; Iteration 2: passes tests
        llm.chat.side_effect = [
            {"content": "// FILE: src/widget.ts\n```typescript\nv1\n```\n"},
            {"content": "// FILE: src/widget.ts\n```typescript\nv2\n```\n"},
        ]

        jira = MagicMock()
        audit = MagicMock(spec=AuditLogger)
        checkpoint = _make_checkpoint()
        from orchestrator.tamper_check import hash_content
        checkpoint.locked_test_hashes = {
            "src/__tests__/widget.test.ts": hash_content("test('renders', () => {});"),
        }

        mock_trigger.return_value = 500
        mock_wait.side_effect = [
            CIResult(
                pipeline_id=500, status="failed", web_url="",
                jobs=[{"name": "test", "status": "failed"}],
            ),
            CIResult(
                pipeline_id=501, status="success", web_url="",
                jobs=[{"name": "test", "status": "success"}],
            ),
        ]

        run(
            context=_make_context(),
            gitlab=gitlab,
            llm=llm,
            jira=jira,
            checkpoint=checkpoint,
            audit=audit,
            max_iterations=6,
            ci_poll_interval=1,
            ci_timeout_minutes=1,
        )

        # Iteration 1: no history clear needed.  Iteration 2: must clear.
        llm.clear_history.assert_called_once_with(keep_system=True)

    @patch("orchestrator.stages.stage3_code_gen.wait_for_pipeline")
    @patch("orchestrator.stages.stage3_code_gen.trigger_ci")
    def test_no_history_clear_on_iteration_1(self, mock_trigger, mock_wait):
        """clear_history must NOT be called when only one iteration runs."""
        gitlab = MagicMock()
        gitlab.branch_exists.return_value = True
        gitlab.create_mr.return_value = MergeRequestInfo(
            iid=98, web_url="", state="opened", draft=True,
        )
        gitlab.commit_files.return_value = "sha_n1b"
        gitlab.get_file_content.return_value = "test('renders', () => {});"

        llm = MagicMock()
        llm.chat.return_value = {
            "content": "// FILE: src/widget.ts\n```typescript\nv1\n```\n"
        }

        jira = MagicMock()
        audit = MagicMock(spec=AuditLogger)
        checkpoint = _make_checkpoint()
        from orchestrator.tamper_check import hash_content
        checkpoint.locked_test_hashes = {
            "src/__tests__/widget.test.ts": hash_content("test('renders', () => {});"),
        }

        mock_trigger.return_value = 600
        mock_wait.return_value = CIResult(
            pipeline_id=600, status="success", web_url="",
            jobs=[{"name": "test", "status": "success"}],
        )

        run(
            context=_make_context(),
            gitlab=gitlab,
            llm=llm,
            jira=jira,
            checkpoint=checkpoint,
            audit=audit,
            max_iterations=6,
            ci_poll_interval=1,
            ci_timeout_minutes=1,
        )

        llm.clear_history.assert_not_called()
