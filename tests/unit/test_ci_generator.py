"""Unit tests for target repository CI workflow generator."""

from unittest.mock import MagicMock
import pytest

from orchestrator.ci_generator import (
    ensure_target_ci_workflow,
    generate_target_repo_ci,
    get_target_ci_filename,
    get_target_repo_name,
    has_target_ci_workflow,
)


def test_get_target_repo_name():
    assert get_target_repo_name("pchhabra07/Full-Stack-To-Do-List") == "Full-Stack-To-Do-List"
    assert get_target_repo_name("my-org/backend-service.git") == "backend-service"
    assert get_target_repo_name("single-repo") == "single-repo"


def test_get_target_ci_filename():
    assert get_target_ci_filename("pchhabra07/Full-Stack-To-Do-List") == "full-stack-to-do-list-ci.yml"
    assert get_target_ci_filename("org/My_Service.git") == "my_service-ci.yml"
    assert get_target_ci_filename("") == "target-repo-ci.yml"


def test_generate_target_repo_ci():
    path, content = generate_target_repo_ci("pchhabra07/Full-Stack-To-Do-List")
    assert path == ".github/workflows/full-stack-to-do-list-ci.yml"
    assert "Full-Stack-To-Do-List CI Validation" in content
    assert "automation/**" in content
    assert "@testing-library/dom" in content


def test_has_target_ci_workflow_from_file_paths():
    all_paths = [
        "src/index.js",
        ".github/workflows/ci.yml",
    ]
    exists, path = has_target_ci_workflow(MagicMock(), "main", all_file_paths=all_paths)
    assert exists is True
    assert path == ".github/workflows/ci.yml"


def test_has_target_ci_workflow_from_vcs_tree():
    client = MagicMock()
    entry = MagicMock()
    entry.path = ".github/workflows/test.yml"
    client.get_file_tree.return_value = [entry]

    exists, path = has_target_ci_workflow(client, "main", all_file_paths=[])
    assert exists is True
    assert path == ".github/workflows/test.yml"


def test_ensure_target_ci_workflow_creates_when_missing():
    client = MagicMock()
    client.get_file_tree.return_value = []

    path, created = ensure_target_ci_workflow(
        client,
        branch="automation/1/tests",
        repo_identifier="pchhabra07/Full-Stack-To-Do-List",
        all_file_paths=[],
    )

    assert path == ".github/workflows/full-stack-to-do-list-ci.yml"
    assert created is True
    client.commit_files.assert_called_once()
    args, kwargs = client.commit_files.call_args
    assert kwargs["branch"] == "automation/1/tests"
    assert ".github/workflows/full-stack-to-do-list-ci.yml" in kwargs["files"]


def test_ensure_target_ci_workflow_skips_when_existing():
    client = MagicMock()
    all_paths = [".github/workflows/my-ci.yml"]

    path, created = ensure_target_ci_workflow(
        client,
        branch="automation/1/tests",
        repo_identifier="pchhabra07/Full-Stack-To-Do-List",
        all_file_paths=all_paths,
    )

    assert path == ".github/workflows/my-ci.yml"
    assert created is False
    client.commit_files.assert_not_called()
