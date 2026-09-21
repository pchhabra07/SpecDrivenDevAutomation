"""
Unit tests for Stage 1 qualification heuristics and triage rejection (Phase 4).
"""

from unittest.mock import MagicMock

import pytest

from orchestrator.jira_client import TicketData
from orchestrator.stages.stage1_context import _check_qualification, run


class TestQualificationTriage:
    def test_eligible_ticket(self):
        ticket = TicketData(
            ticket_id="PROJ-1",
            summary="Fix navbar button alignment",
            description="Button alignment issue in header",
            acceptance_criteria="Button is centered at 12px padding",
            labels=["SpecDrivenDev"],
        )
        assert _check_qualification(ticket) is None

    def test_database_migration_rejected(self):
        ticket = TicketData(
            ticket_id="PROJ-2",
            summary="Execute database migration for user table",
            description="Alter table add column age integer",
            acceptance_criteria="Column added",
            labels=["SpecDrivenDev"],
        )
        reason = _check_qualification(ticket)
        assert reason is not None
        assert "migration" in reason.lower() or "schema" in reason.lower()

    def test_missing_acceptance_criteria_rejected(self):
        ticket = TicketData(
            ticket_id="PROJ-3",
            summary="Fix button",
            description="Fix button",
            acceptance_criteria="",
            labels=["SpecDrivenDev"],
        )
        reason = _check_qualification(ticket)
        assert reason == "Missing acceptance criteria"

    def test_run_rejects_ineligible_ticket(self):
        mock_jira = MagicMock()
        mock_jira.get_ticket.return_value = TicketData(
            ticket_id="PROJ-4",
            summary="Run db migration script",
            description="Database migration",
            acceptance_criteria="Done",
            labels=["SpecDrivenDev"],
        )
        mock_gitlab = MagicMock()
        mock_llm = MagicMock()
        mock_checkpoint = MagicMock()
        mock_audit = MagicMock()

        with pytest.raises(ValueError, match="is ineligible for automation"):
            run(
                ticket_id="PROJ-4",
                jira=mock_jira,
                gitlab=mock_gitlab,
                llm=mock_llm,
                checkpoint=mock_checkpoint,
                audit=mock_audit,
                qualifying_label="SpecDrivenDev",
            )

        mock_jira.remove_label.assert_called_once_with("PROJ-4", "SpecDrivenDev")
        mock_jira.post_lifecycle_comment.assert_called_once()
        mock_audit.log.assert_called_with(
            stage=1,
            event_type="stage1_triaged_ineligible",
            payload={"reason": "Database schema migration detected"},
        )
