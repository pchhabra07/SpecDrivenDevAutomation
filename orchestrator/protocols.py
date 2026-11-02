"""
Protocols and interfaces for VCS/SCM and Issue Tracker clients.

Provides static type contracts and runtime-checkable interfaces for
GitHub, GitLab, and JIRA client implementations (S5-4).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IssueTrackerClient(Protocol):
    """Interface for all issue tracker clients (GitHub Issues, JIRA)."""

    def get_ticket(self, ticket_id: str) -> Any: ...

    def post_lifecycle_comment(
        self,
        ticket_id: str,
        title: str,
        message: str,
        mr_url: str | None = None,
    ) -> None: ...

    def transition_ticket(self, ticket_id: str, transitions: list[str]) -> bool: ...

    def remove_label(self, ticket_id: str, label: str) -> None: ...


@runtime_checkable
class VCSClient(Protocol):
    """Interface for all VCS/SCM clients (GitHub, GitLab)."""

    def get_default_branch(self) -> str: ...

    def get_file_tree(self, ref: str | None = None, recursive: bool = True) -> list[Any]: ...

    def get_file_content(self, file_path: str, ref: str | None = None) -> str: ...

    def branch_exists(self, branch_name: str) -> bool: ...

    def create_branch(self, branch_name: str, ref: str | None = None) -> None: ...

    def commit_files(
        self,
        branch: str,
        files: dict[str, str],
        commit_message: str,
        *,
        blocked_paths: set[str] | None = None,
    ) -> str: ...

    def create_mr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        draft: bool = True,
    ) -> Any: ...

    def get_pr_approvals(self, pr_number: int) -> dict[str, Any]: ...

    def get_pr_url(self, mr_iid: int) -> str: ...

    def get_pr_notes(self, mr_iid: int) -> list[dict[str, Any]]: ...

    def promote_mr_to_ready(self, mr_iid: int) -> None: ...

    def update_mr_description(self, mr_iid: int, description: str) -> None: ...

    def add_mr_label(self, mr_iid: int, label: str) -> None: ...
