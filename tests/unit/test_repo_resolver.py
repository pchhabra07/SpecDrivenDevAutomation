"""
Unit tests for orchestrator.repo_resolver (Phase 5).
"""

from orchestrator.jira_client import TicketData
from orchestrator.repo_resolver import RepoResolver


class TestRepoResolver:
    def test_default_fallback_on_empty_mapping(self):
        resolver = RepoResolver("{}")
        ticket = TicketData(
            ticket_id="CORE-100",
            summary="Bug",
            description="Fix",
            acceptance_criteria="AC",
            labels=["spec-driven-dev"],
        )
        assert resolver.resolve(ticket, default_repo_id=42) == 42

    def test_component_match(self):
        resolver = RepoResolver('{"Checkout": 101, "Payments": 102}')
        ticket = TicketData(
            ticket_id="CORE-101",
            summary="Bug",
            description="Fix",
            acceptance_criteria="AC",
            components=["payments"],
        )
        assert resolver.resolve(ticket, default_repo_id=42) == 102

    def test_label_direct_match(self):
        resolver = RepoResolver('{"auth-service": 201}')
        ticket = TicketData(
            ticket_id="CORE-102",
            summary="Bug",
            description="Fix",
            acceptance_criteria="AC",
            labels=["spec-driven-dev", "AUTH-SERVICE"],
        )
        assert resolver.resolve(ticket, default_repo_id=42) == 201

    def test_label_repo_prefix(self):
        resolver = RepoResolver('{"inventory": 301}')
        ticket1 = TicketData(
            ticket_id="CORE-103",
            summary="Bug",
            description="Fix",
            acceptance_criteria="AC",
            labels=["repo:inventory"],
        )
        assert resolver.resolve(ticket1, default_repo_id=42) == 301

        ticket2 = TicketData(
            ticket_id="CORE-104",
            summary="Bug",
            description="Fix",
            acceptance_criteria="AC",
            labels=["repo:9999"],
        )
        assert resolver.resolve(ticket2, default_repo_id=42) == 9999

    def test_project_key_match(self):
        resolver = RepoResolver('{"billing": 401}')
        ticket = TicketData(
            ticket_id="BILLING-55",
            summary="Bug",
            description="Fix",
            acceptance_criteria="AC",
        )
        assert resolver.resolve(ticket, default_repo_id=42) == 401

    def test_invalid_json_safely_ignored(self):
        resolver = RepoResolver("INVALID_JSON{")
        ticket = TicketData(
            ticket_id="CORE-105",
            summary="Bug",
            description="Fix",
            acceptance_criteria="AC",
        )
        assert resolver.resolve(ticket, default_repo_id=42) == 42
        assert resolver.mapping == {}

    def test_stage1_integration_switches_project(self, mocker):
        from unittest.mock import MagicMock
        from orchestrator.checkpoint import Checkpoint
        from orchestrator.stages.stage1_context import run

        mock_jira = MagicMock()
        mock_jira.get_ticket.return_value = TicketData(
            ticket_id="PAY-101",
            summary="Fix payment retry button",
            description="Fix button",
            acceptance_criteria="Retry button functions",
            labels=["SpecDrivenDev"],
            components=["Payments"],
        )

        mock_gitlab = MagicMock()
        mock_gitlab.project_id = 100
        mock_gitlab.get_default_branch.return_value = "main"
        mock_gitlab.get_file_tree.return_value = []

        mock_llm = MagicMock()
        mock_audit = MagicMock()
        checkpoint = Checkpoint(ticket_id="PAY-101")

        resolver = RepoResolver('{"payments": 888}')

        run(
            ticket_id="PAY-101",
            jira=mock_jira,
            gitlab=mock_gitlab,
            llm=mock_llm,
            checkpoint=checkpoint,
            audit=mock_audit,
            qualifying_label="SpecDrivenDev",
            repo_resolver=resolver,
        )

        mock_gitlab.switch_project.assert_called_once_with(888)
        assert checkpoint.repo_id == 888
