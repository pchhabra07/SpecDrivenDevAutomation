"""
Unit tests for orchestrator.github_client.
"""

from unittest.mock import MagicMock, patch

import pytest
from github import GithubException

from orchestrator.github_client import FileEntry, GitHubClient, PullRequestInfo


class TestGitHubClient:
    @patch("orchestrator.github_client.Github")
    def test_init_and_switch_repo(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_repo.full_name = "owner/test-repo"
        mock_gh.get_repo.return_value = mock_repo

        client = GitHubClient(token="ghp_test123", repo="owner/test-repo")
        assert client.repo_name == "owner/test-repo"
        assert client.project_id == "owner/test-repo"

        mock_repo2 = MagicMock()
        mock_repo2.full_name = "owner/other-repo"
        mock_gh.get_repo.return_value = mock_repo2

        client.switch_repo("owner/other-repo")
        assert client.repo_name == "owner/other-repo"

    @patch("orchestrator.github_client.Github")
    def test_get_file_tree(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_branch = MagicMock()
        mock_branch.commit.sha = "abc12345"
        mock_repo.get_branch.return_value = mock_branch

        item1 = MagicMock()
        item1.path = "src/app.py"
        item1.type = "blob"

        item2 = MagicMock()
        item2.path = "tests/test_app.py"
        item2.type = "blob"

        mock_tree = MagicMock()
        mock_tree.tree = [item1, item2]
        mock_repo.get_git_tree.return_value = mock_tree

        client = GitHubClient(token="ghp_test", repo="owner/repo")
        tree = client.get_file_tree(ref="feature-1", path="src/")

        assert len(tree) == 1
        assert tree[0].path == "src/app.py"
        assert tree[0].type == "blob"
        mock_repo.get_git_tree.assert_called_once_with(sha="abc12345", recursive=True)

    @patch("orchestrator.github_client.Github")
    def test_get_file_content(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_content = MagicMock()
        mock_content.decoded_content = b"print('hello world')\n"
        mock_repo.get_contents.return_value = mock_content

        client = GitHubClient(token="ghp_test", repo="owner/repo")
        content = client.get_file_content("main.py", ref="main")

        assert content == "print('hello world')\n"
        mock_repo.get_contents.assert_called_once_with("main.py", ref="main")

    @patch("orchestrator.github_client.Github")
    def test_create_branch_and_exists(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_base = MagicMock()
        mock_base.commit.sha = "base_sha_123"
        mock_repo.get_branch.return_value = mock_base

        client = GitHubClient(token="ghp_test", repo="owner/repo")
        client.create_branch("feature-branch", ref="main")

        mock_repo.create_git_ref.assert_called_once_with(
            ref="refs/heads/feature-branch", sha="base_sha_123"
        )

        # branch_exists true
        assert client.branch_exists("feature-branch") is True

        # branch_exists false
        mock_repo.get_branch.side_effect = GithubException(status=404, data={"message": "Not Found"})
        assert client.branch_exists("non-existent") is False

    @patch("orchestrator.github_client.Github")
    def test_commit_files_tamper_prevention(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        client = GitHubClient(token="ghp_test", repo="owner/repo")
        files = {
            "src/service.py": "code",
            "tests/test_service.py": "tampered test",
        }
        blocked = {"tests/test_service.py"}

        with pytest.raises(ValueError, match="attempt to modify locked files"):
            client.commit_files("feature", files, "feat: update", blocked_paths=blocked)

    @patch("orchestrator.github_client.Github")
    def test_commit_files_atomic_git_data_api(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        target_branch = MagicMock()
        target_branch.commit.commit.tree = "base_tree_sha"
        mock_repo.get_branch.return_value = target_branch

        mock_tree = MagicMock()
        mock_repo.create_git_tree.return_value = mock_tree

        mock_commit = MagicMock()
        mock_commit.sha = "new_commit_sha_999"
        mock_repo.create_git_commit.return_value = mock_commit

        mock_ref = MagicMock()
        mock_repo.get_git_ref.return_value = mock_ref

        client = GitHubClient(token="ghp_test", repo="owner/repo")
        sha = client.commit_files(
            branch="feature",
            files={"src/main.py": "print('ok')"},
            commit_message="feat: init",
        )

        assert sha == "new_commit_sha_999"
        mock_repo.create_git_tree.assert_called_once()
        mock_repo.create_git_commit.assert_called_once()
        mock_ref.edit.assert_called_once_with("new_commit_sha_999")

    @patch("orchestrator.github_client.Github")
    def test_create_pr_and_promote(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_pr = MagicMock()
        mock_pr.number = 42
        mock_pr.html_url = "https://github.com/owner/repo/pull/42"
        mock_pr.state = "open"
        mock_pr.title = "Draft: feat: new spec"
        mock_repo.create_pull.return_value = mock_pr

        client = GitHubClient(token="ghp_test", repo="owner/repo")
        pr_info = client.create_pr(
            source_branch="feature",
            target_branch="main",
            title="feat: new spec",
            description="Details",
            draft=True,
        )

        assert pr_info.number == 42
        assert pr_info.iid == 42
        assert pr_info.draft is True

        # promote to ready
        mock_repo.get_pull.return_value = mock_pr
        client.promote_pr_to_ready(42)
        mock_pr.edit.assert_called_once_with(title="feat: new spec")

    @patch("orchestrator.github_client.Github")
    def test_get_pr_approvals_and_comments(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_pr = MagicMock()
        review1 = MagicMock()
        review1.state = "APPROVED"
        review1.user.login = "alice"
        mock_pr.get_reviews.return_value = [review1]
        mock_repo.get_pull.return_value = mock_pr

        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = []
        mock_repo.get_issue.return_value = mock_issue

        client = GitHubClient(token="ghp_test", repo="owner/repo")
        approvals = client.get_pr_approvals(10)

        assert approvals["approved"] is True
        assert approvals["approved_by"] == ["alice"]
        assert approvals["changes_requested"] is False

    @patch("orchestrator.github_client.Github")
    def test_get_workflow_status_and_trigger(self, mock_gh_cls):
        mock_gh = mock_gh_cls.return_value
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        mock_workflow = MagicMock()
        mock_repo.get_workflow.return_value = mock_workflow

        run1 = MagicMock()
        run1.id = 555
        run1.status = "completed"
        run1.conclusion = "success"
        run1.html_url = "https://github.com/owner/repo/actions/runs/555"

        job1 = MagicMock()
        job1.id = 101
        job1.name = "test"
        job1.conclusion = "success"
        job1.status = "completed"
        job1.html_url = "https://github.com/owner/repo/actions/runs/555/job/101"
        run1.jobs.return_value = [job1]

        mock_repo.get_workflow_runs.return_value = [run1]
        mock_repo.get_workflow_run.return_value = run1

        with patch("orchestrator.github_client.time.sleep"):
            client = GitHubClient(token="ghp_test", repo="owner/repo")
            run_id = client.trigger_workflow("feature")
            assert run_id == 555

            status = client.get_workflow_status(555)
            assert status.pipeline_id == 555
            assert status.status == "success"
            assert len(status.jobs) == 1
            assert status.jobs[0]["name"] == "test"
            assert status.jobs[0]["status"] == "success"
