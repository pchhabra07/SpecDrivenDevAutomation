"""
Target Repository CI Workflow Generator & Manager.

Ensures that target repositories have a properly configured GitHub Actions CI
workflow dynamically named as `<repo_name>-ci.yml` (e.g. `full-stack-to-do-list-ci.yml`),
completely distinct from the orchestrator's own CI workflows.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).parent / "ci_templates" / "target_ci_template.yml"


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
    # Slugify: lowercase and replace non-alphanumeric chars with hyphens
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", repo_name).strip("-").lower()
    if not slug:
        slug = "target-repo"
    return f"{slug}-ci.yml"


def generate_target_repo_ci(
    repo_identifier: str,
    all_file_paths: list[str] | None = None,
) -> tuple[str, str]:
    """
    Generate the CI workflow path and YAML content customized for the target repository.

    Returns
    -------
    tuple[str, str]
        (workflow_path, workflow_content)
        e.g. ('.github/workflows/full-stack-to-do-list-ci.yml', '<yaml content>')
    """
    repo_name = get_target_repo_name(repo_identifier)
    filename = get_target_ci_filename(repo_identifier)
    workflow_path = f".github/workflows/{filename}"

    if _TEMPLATE_PATH.is_file():
        template_content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    else:
        # Fallback inline template if file cannot be read
        template_content = (
            "name: __REPO_NAME__ CI Validation\n"
            "on:\n"
            "  push:\n"
            "    branches: ['automation/**']\n"
            "  workflow_dispatch:\n"
            "permissions:\n"
            "  contents: write\n"
            "  pull-requests: write\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-node@v4\n"
            "        with: { node-version: '20' }\n"
            "      - run: npm install && npm test\n"
        )

    # Customize template with repository name
    content = template_content.replace("__REPO_NAME__", repo_name)
    return workflow_path, content


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
) -> tuple[str, bool]:
    """
    Ensure a CI workflow exists on the given branch of the target repository.
    If none exists, generate and commit `<repo_name>-ci.yml`.

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
    workflow_path, content = generate_target_repo_ci(repo_identifier, all_file_paths)
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
