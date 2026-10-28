"""
Unit tests for Stage 2 fixes and logical edge cases:
- S2-1: Non-blocking Gate 1 (check_approval_once, GatePendingError, checkpoint state, webhook re-enqueue)
- S2-5: Preflight syntax checks & LLM repair on generated tests before commit and on revisions
- S2-3: Content-aware and ticket-label-overridden stack detection
- S2-6: Split test and CI prompt generation with YAML validation fallback
- S2-4: Configurable max revision cycles
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.checkpoint import Checkpoint
from orchestrator.config import Settings
from orchestrator.exceptions import GatePendingError
from orchestrator.gate import (
    GateOutcome,
    GateResult,
    check_approval_once,
)
from orchestrator.models import TicketData
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.stages.stage2_test_gen import (
    _build_ci_prompt,
    _build_ci_update_prompt,
    _build_test_prompt,
    _detect_project_stack,
    _generate_tests,
    _preflight_and_repair_tests,
    run as stage2_run,
)
from webhook.app import _find_ticket_for_pr


def _make_ticket(
    ticket_id: str = "PROJ-101",
    summary: str = "Add dark mode toggle",
    description: str = "Implement dark mode theme switch",
    acceptance_criteria: str = "Must toggle dark-theme class on body",
    labels: list[str] | None = None,
) -> TicketData:
    return TicketData(
        ticket_id=ticket_id,
        summary=summary,
        description=description,
        acceptance_criteria=acceptance_criteria,
        labels=labels or ["SpecDrivenDev"],
    )


def _make_context(
    ticket: TicketData | None = None,
    all_paths: list[str] | None = None,
    source_files: dict[str, str] | None = None,
    test_files: dict[str, str] | None = None,
    llm_selected_paths: list[str] | None = None,
) -> TicketContext:
    t = ticket or _make_ticket()
    return TicketContext(
        ticket=t,
        default_branch="main",
        source_files=source_files or {},
        test_files=test_files or {},
        all_file_paths=all_paths or ["main.py", "tests/test_main.py"],
        llm_selected_paths=llm_selected_paths or [],
    )


# ==============================================================================
# S2-1: Non-Blocking Gate 1 Tests
# ==============================================================================


class TestNonBlockingGate1:
    def test_check_approval_once_github_pending(self):
        mock_client = MagicMock()
        mock_client.get_pr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": False,
        }
        mock_client.get_pr_notes.return_value = []

        res = check_approval_once(mock_client, pr_number=42)
        assert res.outcome == GateOutcome.PENDING
        assert res.approved_by == []
        assert res.reviewer_comments == []

    def test_check_approval_once_github_approved(self):
        mock_client = MagicMock()
        mock_client.get_pr_approvals.return_value = {
            "approved": True,
            "approved_by": ["alice"],
            "changes_requested": False,
        }
        mock_client.get_pr_notes.return_value = []

        res = check_approval_once(mock_client, pr_number=42)
        assert res.outcome == GateOutcome.APPROVED
        assert "alice" in res.approved_by

    def test_check_approval_once_github_changes_requested(self):
        mock_client = MagicMock()
        mock_client.get_pr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": True,
        }
        mock_client.get_pr_notes.return_value = [{"body": "Please add negative tests"}]

        res = check_approval_once(mock_client, pr_number=42)
        assert res.outcome == GateOutcome.CHANGES_REQUESTED
        assert "Please add negative tests" in res.reviewer_comments

    def test_stage2_run_non_blocking_suspends_when_pending(self, tmp_path):
        mock_gitlab = MagicMock()
        mock_gitlab.branch_exists.return_value = False
        mock_gitlab.create_mr.return_value = MagicMock(iid=101)
        mock_gitlab.get_mr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": False,
        }
        mock_gitlab.get_mr_notes.return_value = []

        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "content": "// FILE: tests/test_feature.py\ndef test_feature():\n    assert True\n"
        }
        mock_llm.chat_once.return_value = {
            "content": "// FILE: .github/workflows/ci.yml\nname: CI\non: push\njobs: {}\n"
        }

        mock_audit = MagicMock()
        checkpoint = Checkpoint(ticket_id="PROJ-101")
        ctx = _make_context()

        with pytest.raises(GatePendingError) as exc_info:
            stage2_run(
                context=ctx,
                gitlab=mock_gitlab,
                llm=mock_llm,
                checkpoint=checkpoint,
                audit=mock_audit,
                blocking_gate=False,
                checkpoint_dir=str(tmp_path),
            )

        assert exc_info.value.gate_name == "gate1"
        assert exc_info.value.mr_iid == 101
        assert checkpoint.gate_waiting == "gate1"
        assert checkpoint.gate_waiting_mr_iid == 101
        assert checkpoint.gate_waiting_since is not None
        assert checkpoint.current_stage == 2

    def test_stage2_run_non_blocking_approves_and_clears_gate_waiting(self, tmp_path):
        mock_gitlab = MagicMock()
        mock_gitlab.get_mr_approvals.return_value = {
            "approved": True,
            "approved_by": ["senior-dev"],
            "changes_requested": False,
        }
        mock_gitlab.get_mr_notes.return_value = []

        mock_llm = MagicMock()
        mock_audit = MagicMock()

        # Checkpoint resumed after suspension
        checkpoint = Checkpoint(
            ticket_id="PROJ-101",
            current_stage=2,
            gate1_mr_iid=101,
            gate_waiting="gate1",
            gate_waiting_mr_iid=101,
            generated_test_files={"tests/test_feature.py": "def test_feature(): assert 1 == 1\n"},
        )
        ctx = _make_context()

        locked = stage2_run(
            context=ctx,
            gitlab=mock_gitlab,
            llm=mock_llm,
            checkpoint=checkpoint,
            audit=mock_audit,
            blocking_gate=False,
            checkpoint_dir=str(tmp_path),
        )

        assert "tests/test_feature.py" in locked
        assert checkpoint.gate1_approved is True
        assert checkpoint.current_stage == 3
        assert checkpoint.gate_waiting is None
        assert checkpoint.gate_waiting_mr_iid is None
        assert checkpoint.gate_waiting_since is None
        assert len(checkpoint.locked_test_hashes) == 1

    def test_stage2_run_non_blocking_revision_reparks(self, tmp_path):
        """When reviewer requests changes in non-blocking mode, Stage 2 revises and suspends again."""
        mock_gitlab = MagicMock()
        mock_gitlab.get_mr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": True,
        }
        mock_gitlab.get_mr_notes.return_value = [{"body": "Use absolute selector"}]

        mock_llm = MagicMock()
        # Revision uses chat_once
        mock_llm.chat_once.return_value = {
            "content": "// FILE: tests/test_feature.py\ndef test_feature():\n    assert 2 == 2\n"
        }

        mock_audit = MagicMock()
        checkpoint = Checkpoint(
            ticket_id="PROJ-101",
            current_stage=2,
            gate1_mr_iid=101,
            gate_waiting="gate1",
            gate_waiting_mr_iid=101,
            generated_test_files={"tests/test_feature.py": "def test_feature(): assert 1 == 1\n"},
        )
        ctx = _make_context()

        with pytest.raises(GatePendingError) as exc_info:
            stage2_run(
                context=ctx,
                gitlab=mock_gitlab,
                llm=mock_llm,
                checkpoint=checkpoint,
                audit=mock_audit,
                blocking_gate=False,
                checkpoint_dir=str(tmp_path),
            )

        assert exc_info.value.gate_name == "gate1"
        assert exc_info.value.mr_iid == 101
        assert checkpoint.gate1_revision_count == 1
        assert checkpoint.gate_waiting == "gate1"
        # Commit should have been made for the revision
        mock_gitlab.commit_files.assert_called_once()


class TestWebhookReEnqueue:
    def test_find_ticket_for_pr(self, tmp_path):
        cp_file = tmp_path / "TICK-42.json"
        cp_file.write_text(
            json.dumps({
                "ticket_id": "TICK-42",
                "gate_waiting": "gate1",
                "gate_waiting_mr_iid": 77,
            }),
            encoding="utf-8",
        )

        assert _find_ticket_for_pr(77, str(tmp_path)) == "TICK-42"
        assert _find_ticket_for_pr(99, str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_github_pull_request_review_webhook_reenqueues(self, tmp_path):
        from webhook.app import handle_github_webhook, task_queue

        cp_file = tmp_path / "TICK-50.json"
        cp_file.write_text(
            json.dumps({
                "ticket_id": "TICK-50",
                "gate_waiting": "gate1",
                "gate_waiting_mr_iid": 123,
            }),
            encoding="utf-8",
        )

        while not task_queue.empty():
            task_queue.get_nowait()

        mock_request = AsyncMock()
        mock_request.body.return_value = b"{}"
        mock_request.json.return_value = {
            "action": "submitted",
            "pull_request": {"number": 123},
            "review": {"id": 999, "state": "approved"},
        }

        with patch("webhook.app.get_settings") as mock_settings:
            settings = Settings(
                checkpoint_dir=str(tmp_path),
                github_webhook_secret="",
            )
            mock_settings.return_value = settings

            res = await handle_github_webhook(
                request=mock_request,
                x_hub_signature_256=None,
                x_github_event="pull_request_review",
            )

            assert res["status"] == "requeued"
            assert res["ticket_id"] == "TICK-50"
            queued_ticket = await task_queue.get()
            assert queued_ticket == "TICK-50"


# ==============================================================================
# S2-5: Preflight Syntax Verification and LLM Repair Tests
# ==============================================================================


class TestTestPreflightVerification:
    def test_preflight_passes_valid_python_test(self):
        mock_llm = MagicMock()
        mock_audit = MagicMock()
        valid_tests = {
            "tests/test_calc.py": "def test_add():\n    assert 1 + 1 == 2\n"
        }

        repaired = _preflight_and_repair_tests(
            llm=mock_llm,
            tests=valid_tests,
            max_repairs=2,
            audit=mock_audit,
        )

        assert repaired == valid_tests
        mock_llm.chat_once.assert_not_called()

    def test_preflight_repairs_syntax_error(self):
        mock_llm = MagicMock()
        # Broken syntax: missing closing parenthesis and colon
        broken_tests = {
            "tests/test_calc.py": "def test_add(\n    assert 1 == 1\n"
        }
        # LLM returns valid fixed file
        mock_llm.chat_once.return_value = {
            "content": "// FILE: tests/test_calc.py\ndef test_add():\n    assert 1 == 1\n"
        }
        mock_audit = MagicMock()

        repaired = _preflight_and_repair_tests(
            llm=mock_llm,
            tests=broken_tests,
            max_repairs=2,
            audit=mock_audit,
        )

        assert "def test_add():" in repaired["tests/test_calc.py"]
        mock_llm.chat_once.assert_called_once()
        assert mock_audit.log.call_count >= 2

    def test_preflight_exhausts_attempts_gracefully(self):
        mock_llm = MagicMock()
        broken_tests = {
            "tests/test_calc.py": "def test_add(\n    assert 1 == 1\n"
        }
        # LLM returns still-broken code
        mock_llm.chat_once.return_value = {
            "content": "// FILE: tests/test_calc.py\ndef test_add(\n    assert 2 == 2\n"
        }
        mock_audit = MagicMock()

        repaired = _preflight_and_repair_tests(
            llm=mock_llm,
            tests=broken_tests,
            max_repairs=2,
            audit=mock_audit,
        )

        # Retried twice and returned original/current without crashing
        assert mock_llm.chat_once.call_count == 2
        assert "tests/test_calc.py" in repaired


# ==============================================================================
# S2-3: Content-Aware & Label-Overridden Stack Detection Tests
# ==============================================================================


class TestStackDetectionEnhancements:
    def test_label_override_authoritative_python(self):
        ctx = _make_context(
            ticket=_make_ticket(labels=["SpecDrivenDev", "stack:python"]),
            all_paths=["src/App.tsx", "package.json"],
        )
        assert _detect_project_stack(ctx) == "python"

    def test_label_override_authoritative_react(self):
        ctx = _make_context(
            ticket=_make_ticket(labels=["SpecDrivenDev", "stack:react"]),
            all_paths=["main.py", "requirements.txt"],
        )
        assert _detect_project_stack(ctx) == "react"

    def test_content_inspection_detects_react_in_polyglot(self):
        ctx = _make_context(
            all_paths=["backend/app.py", "frontend/Component.tsx"],
            source_files={
                "frontend/Component.tsx": "import React, { useState } from 'react';\nexport const Comp = () => <div/>;",
            },
        )
        assert _detect_project_stack(ctx) == "react"

    def test_content_inspection_detects_python_pytest(self):
        ctx = _make_context(
            all_paths=["app.py", "static/style.css"],
            source_files={
                "app.py": "import pytest\nfrom fastapi import FastAPI\napp = FastAPI()",
            },
        )
        assert _detect_project_stack(ctx) == "python"


# ==============================================================================
# S2-6: Split Test and CI Generation & YAML Validation Tests
# ==============================================================================


class TestSplitTestAndCIGeneration:
    def test_generates_ci_workflow_via_separate_prompt(self):
        mock_llm = MagicMock()
        # Call 1 (chat): tests only
        mock_llm.chat.return_value = {
            "content": "// FILE: tests/test_demo.py\ndef test_demo(): assert True\n"
        }
        # Call 2 (chat_once): CI workflow
        mock_llm.chat_once.return_value = {
            "content": "// FILE: .github/workflows/ci.yml\nname: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        }

        ctx = _make_context(all_paths=["main.py"])
        tests, ci = _generate_tests(
            llm=mock_llm,
            context=ctx,
            existing_ci_path=None,
            existing_ci_content=None,
            repo_id="my-repo",
        )

        assert "tests/test_demo.py" in tests
        assert ci is not None
        assert ci[0] == ".github/workflows/ci.yml"
        assert "name: CI" in ci[1]
        mock_llm.chat.assert_called_once()
        mock_llm.chat_once.assert_called_once()

    def test_invalid_ci_yaml_falls_back_to_template(self):
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "content": "// FILE: tests/test_demo.py\ndef test_demo(): assert True\n"
        }
        # Malformed YAML
        mock_llm.chat_once.return_value = {
            "content": "// FILE: .github/workflows/ci.yml\nname: CI\njobs: [unclosed: yaml: {\n"
        }

        ctx = _make_context(all_paths=["main.py"])
        tests, ci = _generate_tests(
            llm=mock_llm,
            context=ctx,
            existing_ci_path=None,
            existing_ci_content=None,
            repo_id="my-repo",
        )

        assert "tests/test_demo.py" in tests
        assert ci is not None
        # Valid fallback CI generated
        assert ci[0].startswith(".github/workflows/") and ci[0].endswith(".yml")
        assert "name:" in ci[1]


# ==============================================================================
# S2-4: Configurable Max Revision Cycles Tests
# ==============================================================================


class TestConfigurableRevisionCycles:
    def test_stage2_run_respects_custom_max_revision_cycles(self):
        mock_gitlab = MagicMock()
        mock_gitlab.get_mr_approvals.return_value = {
            "approved": False,
            "approved_by": [],
            "changes_requested": True,
        }
        mock_gitlab.get_mr_notes.return_value = [{"body": "Revise test"}]

        mock_llm = MagicMock()
        mock_llm.chat_once.return_value = {
            "content": "// FILE: tests/test_feature.py\ndef test_feature(): assert True\n"
        }

        checkpoint = Checkpoint(
            ticket_id="PROJ-101",
            current_stage=2,
            gate1_mr_iid=101,
            gate1_revision_count=2,  # Already at cycle 2
            generated_test_files={"tests/test_feature.py": "def test_feature(): assert 1 == 1\n"},
        )
        ctx = _make_context()

        # max_revision_cycles=2 -> next revision would exceed limit
        with pytest.raises(RuntimeError) as exc_info:
            stage2_run(
                context=ctx,
                gitlab=mock_gitlab,
                llm=mock_llm,
                checkpoint=checkpoint,
                audit=MagicMock(),
                max_revision_cycles=2,
                blocking_gate=False,
            )

        assert "not approved after 2 revision cycles" in str(exc_info.value)
