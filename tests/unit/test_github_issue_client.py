"""
Unit tests for orchestrator.github_issue_client.
"""

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.github_issue_client import GitHubIssueClient


class TestGitHubIssueClient:
    def test_parse_issue_number(self):
        assert GitHubIssueClient.parse_issue_number(123) == 123
        assert GitHubIssueClient.parse_issue_number("123") == 123
        assert GitHubIssueClient.parse_issue_number("#123") == 123
        assert GitHubIssueClient.parse_issue_number("PROJ-456") == 456
        assert GitHubIssueClient.parse_issue_number("ISSUE-99") == 99

        with pytest.raises(ValueError, match="Cannot extract GitHub issue number"):
            GitHubIssueClient.parse_issue_number("invalid-no-number")

    @patch("orchestrator.github_issue_client.Github")
    def test_get_ticket_with_acceptance_criteria(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_issue = MagicMock()
        mock_issue.number = 42
        mock_issue.title = "Implement Payment Service"
        mock_issue.body = (
            "User story details.\n\n"
            "## Acceptance Criteria\n"
            "- [ ] Payments must be validated against fraud rules\n"
            "- [ ] Audit log entry created on success\n\n"
            "## Technical Notes\n"
            "Use Stripe API v3"
        )
        lbl1 = MagicMock()
        lbl1.name = "SpecDrivenDev"
        lbl2 = MagicMock()
        lbl2.name = "component:Payments"
        mock_issue.labels = [lbl1, lbl2]
        mock_issue.assignee.login = "developer1"
        mock_issue.user.login = "reporter1"
        mock_issue.state = "open"
        mock_issue.html_url = "https://github.com/owner/repo/issues/42"

        mock_repo.get_issue.return_value = mock_issue

        client = GitHubIssueClient(token="ghp_test", repo="owner/repo")
        ticket = client.get_ticket("42")

        assert ticket.ticket_id == "42"
        assert ticket.summary == "Implement Payment Service"
        assert "Payments must be validated" in ticket.acceptance_criteria
        assert "Use Stripe API v3" not in ticket.acceptance_criteria
        assert "SpecDrivenDev" in ticket.labels
        assert "Payments" in ticket.components
        assert ticket.assignee == "developer1"
        assert ticket.reporter == "reporter1"
        assert ticket.status == "open"

    @patch("orchestrator.github_issue_client.Github")
    def test_get_ticket_checklist_fallback(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_issue = MagicMock()
        mock_issue.number = 10
        mock_issue.title = "Task without header"
        mock_issue.body = (
            "General description.\n"
            "- [ ] Item 1\n"
            "- [x] Item 2\n"
            "Footer info."
        )
        mock_issue.labels = []
        mock_issue.assignee = None
        mock_issue.user = None
        mock_issue.state = "open"
        mock_issue.html_url = "https://github.com/owner/repo/issues/10"
        mock_repo.get_issue.return_value = mock_issue

        client = GitHubIssueClient(token="ghp_test", repo="owner/repo")
        ticket = client.get_ticket(10)

        assert "- [ ] Item 1" in ticket.acceptance_criteria
        assert "- [x] Item 2" in ticket.acceptance_criteria
        assert "General description" not in ticket.acceptance_criteria

    @patch("orchestrator.github_issue_client.Github")
    def test_transition_ticket(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        client = GitHubIssueClient(token="ghp_test", repo="owner/repo")

        # Close transition
        res = client.transition_ticket(15, "Done")
        assert res is True
        mock_issue.edit.assert_called_once_with(state="closed")

        # In-progress label
        res = client.transition_ticket(15, ["In Progress"])
        assert res is True
        mock_issue.add_to_labels.assert_called_with("in-progress")

        # Review label
        res = client.transition_ticket(15, ["Under Review"])
        assert res is True
        mock_issue.add_to_labels.assert_called_with("under-review")

    @patch("orchestrator.github_issue_client.Github")
    def test_remove_label_and_comment(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        client = GitHubIssueClient(token="ghp_test", repo="owner/repo")
        client.remove_label(15, "SpecDrivenDev")
        mock_issue.remove_from_labels.assert_called_once_with("SpecDrivenDev")

        client.post_lifecycle_comment(
            15,
            title="Gate 1 Approved",
            message="Moving to Code Gen",
            mr_url="https://github.com/owner/repo/pull/20",
        )
        mock_issue.create_comment.assert_called_once()
        comment = mock_issue.create_comment.call_args[0][0]
        assert "Gate 1 Approved" in comment
        assert "Moving to Code Gen" in comment
        assert "https://github.com/owner/repo/pull/20" in comment
