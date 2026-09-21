"""
Unit tests for orchestrator.jira_client enhancements (Phase 3).
"""

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.jira_client import JiraClient


class TestJiraClientTransitions:
    @patch("orchestrator.jira_client.Jira")
    def test_find_transition_exact_match(self, mock_jira_cls):
        mock_jira = mock_jira_cls.return_value
        mock_jira.get_issue_transitions.return_value = [
            {"id": "11", "name": "To Do"},
            {"id": "21", "name": "In Progress"},
            {"id": "31", "name": "Under Review"},
        ]

        client = JiraClient("https://jira.test", "user@test.com", "token123")
        res = client.find_transition("PROJ-1", ["In Review", "Under Review"])

        assert res is not None
        trans_id, name = res
        assert trans_id == "31"
        assert name == "Under Review"

    @patch("orchestrator.jira_client.Jira")
    def test_find_transition_case_insensitive(self, mock_jira_cls):
        mock_jira = mock_jira_cls.return_value
        mock_jira.get_issue_transitions.return_value = [
            {"id": "21", "name": "in progress"},
        ]

        client = JiraClient("https://jira.test", "user@test.com", "token123")
        res = client.find_transition("PROJ-1", ["In Progress"])

        assert res is not None
        assert res[0] == "21"

    @patch("orchestrator.jira_client.Jira")
    def test_find_transition_none_match(self, mock_jira_cls):
        mock_jira = mock_jira_cls.return_value
        mock_jira.get_issue_transitions.return_value = [
            {"id": "11", "name": "Closed"},
        ]

        client = JiraClient("https://jira.test", "user@test.com", "token123")
        res = client.find_transition("PROJ-1", ["Under Review", "In Progress"])

        assert res is None

    @patch("orchestrator.jira_client.Jira")
    def test_transition_ticket_success(self, mock_jira_cls):
        mock_jira = mock_jira_cls.return_value
        mock_jira.get_issue_transitions.return_value = [
            {"id": "31", "name": "Under Review"},
        ]

        client = JiraClient("https://jira.test", "user@test.com", "token123")
        success = client.transition_ticket("PROJ-1", ["Under Review"])

        assert success is True
        mock_jira.set_issue_status.assert_called_once_with("PROJ-1", "31")

    @patch("orchestrator.jira_client.Jira")
    def test_transition_ticket_fallback(self, mock_jira_cls):
        mock_jira = mock_jira_cls.return_value
        mock_jira.get_issue_transitions.return_value = [
            {"id": "41", "name": "In Review"},
        ]

        client = JiraClient("https://jira.test", "user@test.com", "token123")
        success = client.transition_ticket("PROJ-1", ["Under Review", "In Review"])

        assert success is True
        mock_jira.set_issue_status.assert_called_once_with("PROJ-1", "41")

    @patch("orchestrator.jira_client.Jira")
    def test_remove_label(self, mock_jira_cls):
        mock_jira = mock_jira_cls.return_value
        mock_jira.issue.return_value = {
            "fields": {"labels": ["SpecDrivenDev", "Frontend", "Bug"]}
        }

        client = JiraClient("https://jira.test", "user@test.com", "token123")
        client.remove_label("PROJ-1", "SpecDrivenDev")

        mock_jira.update_issue_field.assert_called_once_with(
            "PROJ-1", {"labels": ["Frontend", "Bug"]}
        )

    @patch("orchestrator.jira_client.Jira")
    def test_post_lifecycle_comment(self, mock_jira_cls):
        mock_jira = mock_jira_cls.return_value

        client = JiraClient("https://jira.test", "user@test.com", "token123")
        client.post_lifecycle_comment(
            "PROJ-1",
            "Gate 2 Ready",
            "Please review diffs.",
            mr_url="https://gitlab.test/mr/10",
        )

        mock_jira.issue_add_comment.assert_called_once()
        body = mock_jira.issue_add_comment.call_args[0][1]
        assert "Gate 2 Ready" in body
        assert "https://gitlab.test/mr/10" in body
