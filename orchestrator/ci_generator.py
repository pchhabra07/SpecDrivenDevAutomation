"""
Target Repository CI Workflow Generator & Manager.

Ensures that target repositories have a properly configured GitHub Actions CI
workflow dynamically tailored to the repository's stack, or adapts an existing
repository CI workflow. Completely distinct from the orchestrator's own CI workflows.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_target_repo_name(repo_identifier: str) -> str:
    """Extract clean repository name (e.g., 'Full-Stack-To-Do-List')."""
    clean = str(repo_identifier).strip().replace("\\", "/")
    if "/" in clean:
        clean = clean.split("/")[-1]
    return clean.removesuffix(".git").strip()


def get_target_ci_filename(repo_identifier: str) -> str:
    """
    Return the dynamic CI workflow filename for the target repository.
    Example: 'pchhabra07/Full-Stack-To-Do-List' -> 'full-stack-to-do-list-ci.yml'.
    """
    repo_name = get_target_repo_name(repo_identifier)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", repo_name).strip("-").lower()
    if not slug:
        slug = "target-repo"
    return f"{slug}-ci.yml"


def find_existing_ci_workflow(
    gitlab: Any,
    branch: str,
    all_file_paths: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """
    Find existing CI workflow on *branch* and return its path and content.

    Returns
    -------
    tuple[str | None, str | None]
        (workflow_path, workflow_content) or (None, None)
    """
    exists, path = has_target_ci_workflow(gitlab, branch, all_file_paths)
    if not exists or not path:
        return None, None

    content = None
    try:
        if hasattr(gitlab, "get_file_content"):
            content = gitlab.get_file_content(path, ref=branch)
    except Exception as exc:
        logger.debug("Could not read existing CI workflow content from %s on %s: %s", path, branch, exc)

    return path, content


def generate_fallback_ci(
    repo_identifier: str,
    stack: str = "generic",
) -> tuple[str, str]:
    """
    Generate a clean, minimal fallback GitHub Actions workflow tailored to *stack*.
    Used only when the LLM fails to output a repo-specific CI workflow.
    """
    repo_name = get_target_repo_name(repo_identifier)
    filename = get_target_ci_filename(repo_identifier)
    workflow_path = f".github/workflows/{filename}"

    if stack == "python":
        test_step = (
            "      - name: Setup Python\n"
            "        uses: actions/setup-python@v5\n"
            "        with:\n"
            "          python-version: '3.11'\n"
            "      - name: Install dependencies & run tests\n"
            "        run: |\n"
            "          python -m pip install --upgrade pip pytest\n"
            "          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi\n"
            "          pytest -v\n"
        )
    elif stack in ("node", "react"):
        test_step = (
            "      - name: Setup Node.js\n"
            "        uses: actions/setup-node@v4\n"
            "        with:\n"
            "          node-version: '20'\n"
            "      - name: Install dependencies & run tests\n"
            "        run: |\n"
            "          if [ -f package.json ]; then npm install || true; npm test; else echo 'No package.json'; fi\n"
        )
    else:
        test_step = (
            "      - name: Run Test Suite\n"
            "        run: |\n"
            "          if [ -f package.json ]; then\n"
            "            npm install || true\n"
            "            npm test\n"
            "          elif [ -f requirements.txt ] || [ -f pyproject.toml ]; then\n"
            "            python -m pip install --upgrade pytest || true\n"
            "            pytest -v\n"
            "          else\n"
            "            echo 'No standard test runner configuration found.'\n"
            "          fi\n"
        )

    content = (
        f"name: {repo_name} CI Validation\n"
        f"\n"
        f"on:\n"
        f"  push:\n"
        f"    branches: ['automation/**']\n"
        f"  workflow_dispatch:\n"
        f"    inputs:\n"
        f"      test_only:\n"
        f"        description: 'Run test job only'\n"
        f"        required: false\n"
        f"        default: 'false'\n"
        f"\n"
        f"concurrency:\n"
        f"  group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}\n"
        f"  cancel-in-progress: true\n"
        f"\n"
        f"permissions:\n"
        f"  contents: write\n"
        f"  pull-requests: write\n"
        f"\n"
        f"jobs:\n"
        f"  test:\n"
        f"    name: test\n"
        f"    runs-on: ubuntu-latest\n"
        f"    steps:\n"
        f"      - name: Checkout Code\n"
        f"        uses: actions/checkout@v4\n"
        f"{test_step}"
    )

    return workflow_path, content


def generate_target_repo_ci(
    repo_identifier: str,
    all_file_paths: list[str] | None = None,
    stack: str = "generic",
) -> tuple[str, str]:
    """
    Generate the CI workflow path and YAML content for the target repository.
    Prefers clean stack-tailored workflow generation.
    """
    return generate_fallback_ci(repo_identifier, stack=stack)


def has_target_ci_workflow(
    gitlab: Any,
    branch: str,
    all_file_paths: list[str] | None = None,
) -> tuple[bool, str | None]:
    """
    Check if the target repository has an existing CI workflow on *branch*.

    Returns
    -------
    tuple[bool, str | None]
        (exists, existing_workflow_path)
    """
    # 1. Check all_file_paths if provided
    if all_file_paths:
        for p in all_file_paths:
            normalized = p.replace("\\", "/")
            if normalized.startswith(".github/workflows/") and (
                normalized.endswith(".yml") or normalized.endswith(".yaml")
            ):
                return True, normalized
            if normalized == ".gitlab-ci.yml":
                return True, normalized

    # 2. Query VCS client tree
    try:
        tree = gitlab.get_file_tree(ref=branch, path=".github/workflows")
        for entry in tree:
            p = entry.path.replace("\\", "/")
            if p.endswith(".yml") or p.endswith(".yaml"):
                return True, p
    except Exception as exc:
        logger.debug("Could not inspect .github/workflows on %s: %s", branch, exc)

    return False, None


def ensure_target_ci_workflow(
    gitlab: Any,
    branch: str,
    repo_identifier: str,
    all_file_paths: list[str] | None = None,
    stack: str = "generic",
) -> tuple[str, bool]:
    """
    Ensure a CI workflow exists on the given branch of the target repository.
    If none exists, generate and commit a fallback workflow.

    Returns
    -------
    tuple[str, bool]
        (workflow_path, was_created)
    """
    exists, existing_path = has_target_ci_workflow(gitlab, branch, all_file_paths)
    if exists and existing_path:
        logger.info("Found existing CI workflow on %s: %s", branch, existing_path)
        return existing_path, False

    # Generate customized workflow
    workflow_path, content = generate_target_repo_ci(repo_identifier, all_file_paths, stack=stack)
    logger.info(
        "No CI workflow found on %s; creating dynamically named workflow: %s",
        branch,
        workflow_path,
    )

    try:
        gitlab.commit_files(
            branch=branch,
            files={workflow_path: content},
            commit_message=f"[spec-driven] Add CI validation workflow: {os.path.basename(workflow_path)}",
        )
        return workflow_path, True
    except Exception as exc:
        logger.warning(
            "Failed to commit dynamic CI workflow %s on %s: %s",
            workflow_path,
            branch,
            exc,
        )
        return workflow_path, False
