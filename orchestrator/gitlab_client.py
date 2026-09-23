"""
GitLab API client — wraps ``python-gitlab`` for all repository operations.

Key operations: file tree enumeration, file reads, branch creation,
multi-file commits, MR creation / promotion / labelling, and CI/CD
pipeline triggering.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

import gitlab

logger = logging.getLogger(__name__)


@dataclass
class FileEntry:
    """A single file from the repository tree."""

    path: str
    type: str  # "blob" or "tree"


@dataclass
class MergeRequestInfo:
    """Essential metadata about a created or fetched MR."""

    iid: int
    web_url: str
    state: str
    draft: bool


@dataclass
class PipelineResult:
    """Terminal result of a CI/CD pipeline run."""

    pipeline_id: int
    status: str  # "success", "failed", "canceled"
    web_url: str
    jobs: list[dict[str, Any]] = field(default_factory=list)


class GitLabClient:
    """High-level GitLab API facade."""

    def __init__(self, base_url: str, private_token: str, repo_id: int) -> None:
        self._gl = gitlab.Gitlab(base_url, private_token=private_token)
        self._project = self._gl.projects.get(repo_id)
        logger.info("GitLab client initialised for project %s", getattr(self._project, "path_with_namespace", str(repo_id)))

    @property
    def project_id(self) -> int:
        """Return the current active project ID."""
        return getattr(self._project, "id", None)

    def switch_project(self, repo_id: int) -> None:
        """Switch active repository/project ID."""
        self._project = self._gl.projects.get(repo_id)
        logger.info("GitLab client switched to project %s", getattr(self._project, "path_with_namespace", str(repo_id)))

    # ── Repository reads ────────────────────────────────────────────────

    def get_file_tree(
        self,
        ref: str = "main",
        path: str = "",
        recursive: bool = True,
    ) -> list[FileEntry]:
        """Return the full file tree for *ref* under *path*."""
        items = self._project.repository_tree(
            path=path, ref=ref, recursive=recursive, per_page=100, get_all=True,
        )
        return [FileEntry(path=i["path"], type=i["type"]) for i in items]

    def get_file_content(self, file_path: str, ref: str = "main") -> str:
        """Return the decoded UTF-8 content of a single file."""
        f = self._project.files.get(file_path=file_path, ref=ref)
        return base64.b64decode(f.content).decode("utf-8")

    def get_default_branch(self) -> str:
        """Return the name of the project's default branch."""
        return self._project.default_branch

    # ── Branch management ───────────────────────────────────────────────

    def create_branch(self, branch_name: str, ref: str = "main") -> None:
        """Create a new branch from *ref*."""
        self._project.branches.create({"branch": branch_name, "ref": ref})
        logger.info("Branch created: %s (from %s)", branch_name, ref)

    def branch_exists(self, branch_name: str) -> bool:
        """Check whether a branch already exists."""
        try:
            self._project.branches.get(branch_name)
            return True
        except gitlab.exceptions.GitlabGetError:
            return False

    def delete_file(self, path: str, branch: str, message: str) -> bool:
        """Delete a file from *branch* if it exists."""
        try:
            self._project.files.delete(file_path=path, branch=branch, commit_message=message)
            logger.info("Deleted %s from branch %s", path, branch)
            return True
        except Exception as exc:
            logger.debug("Could not delete %s from %s: %s", path, branch, exc)
            return False

    # ── Commits ─────────────────────────────────────────────────────────

    def commit_files(
        self,
        branch: str,
        files: dict[str, str],
        commit_message: str,
        *,
        blocked_paths: set[str] | None = None,
    ) -> str:
        """
        Create a single commit that creates/updates multiple files.

        Parameters
        ----------
        branch:
            Target branch.
        files:
            ``{file_path: content_string}`` — each file is created or
            updated.
        commit_message:
            Commit message.
        blocked_paths:
            If provided, raise ``ValueError`` if any key in *files*
            matches a blocked path (used to enforce test-file locking).

        Returns
        -------
        str
            The resulting commit SHA.
        """
        if blocked_paths:
            violations = set(files.keys()) & blocked_paths
            if violations:
                raise ValueError(
                    f"Commit rejected — attempt to modify locked files: {violations}"
                )

        actions = []
        for path, content in files.items():
            # Determine action: update if file exists, create otherwise.
            try:
                self._project.files.get(file_path=path, ref=branch)
                action = "update"
            except gitlab.exceptions.GitlabGetError:
                action = "create"
            actions.append({
                "action": action,
                "file_path": path,
                "content": content,
            })

        commit = self._project.commits.create({
            "branch": branch,
            "commit_message": commit_message,
            "actions": actions,
        })
        sha = commit.id
        logger.info("Commit %s on %s (%d files)", sha[:10], branch, len(actions))
        return sha

    # ── Merge Requests ──────────────────────────────────────────────────

    def create_mr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        draft: bool = True,
    ) -> MergeRequestInfo:
        """Create a new merge request (draft by default)."""
        mr_title = f"Draft: {title}" if draft else title
        mr = self._project.mergerequests.create({
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": mr_title,
            "description": description,
        })
        info = MergeRequestInfo(
            iid=mr.iid,
            web_url=mr.web_url,
            state=mr.state,
            draft=draft,
        )
        logger.info("MR !%d created: %s", info.iid, info.web_url)
        return info

    def get_mr_approvals(self, mr_iid: int) -> dict[str, Any]:
        """
        Fetch the approvals state of a merge request.

        Returns the raw approvals dict from the GitLab API, including
        ``approved``, ``approved_by``, etc.
        """
        mr = self._project.mergerequests.get(mr_iid)
        approvals = mr.approvals.get()
        return {
            "approved": approvals.approved,
            "approved_by": [
                a["user"]["username"] for a in getattr(approvals, "approved_by", [])
            ],
        }

    def update_mr_description(self, mr_iid: int, description: str) -> None:
        """Update the description of an existing MR."""
        mr = self._project.mergerequests.get(mr_iid)
        mr.description = description
        mr.save()
        logger.info("MR !%d description updated", mr_iid)

    def promote_mr_to_ready(self, mr_iid: int) -> None:
        """Remove 'Draft:' prefix from MR title to promote it to ready."""
        mr = self._project.mergerequests.get(mr_iid)
        if mr.title.startswith("Draft: "):
            mr.title = mr.title.removeprefix("Draft: ")
            mr.save()
            logger.info("MR !%d promoted to ready-for-review", mr_iid)

    def add_mr_label(self, mr_iid: int, label: str) -> None:
        """Add a label to a merge request."""
        mr = self._project.mergerequests.get(mr_iid)
        current_labels = mr.labels or []
        if label not in current_labels:
            mr.labels = current_labels + [label]
            mr.save()
            logger.info("MR !%d labelled: %s", mr_iid, label)

    def merge_mr(self, mr_iid: int, squash: bool = True) -> None:
        """Merge a merge request (squash by default)."""
        mr = self._project.mergerequests.get(mr_iid)
        mr.merge(squash=squash)
        logger.info("MR !%d merged (squash=%s)", mr_iid, squash)

    def get_mr_notes(self, mr_iid: int) -> list[dict[str, Any]]:
        """Fetch all notes/comments on an MR (for reviewer feedback routing)."""
        mr = self._project.mergerequests.get(mr_iid)
        notes = mr.notes.list(get_all=True)
        return [
            {
                "author": n.author["username"],
                "body": n.body,
                "created_at": n.created_at,
                "system": n.system,
            }
            for n in notes
            if not n.system  # skip system-generated notes
        ]

    def add_mr_comment(self, mr_iid: int, comment: str) -> None:
        """Add a comment/note to a Merge Request."""
        mr = self._project.mergerequests.get(mr_iid)
        mr.notes.create({"body": comment})
        logger.info("Comment posted to MR !%d", mr_iid)

    # ── CI/CD Pipelines ────────────────────────────────────────────────

    def trigger_pipeline(
        self,
        branch: str,
        variables: dict[str, str] | None = None,
    ) -> int:
        """Trigger a new pipeline on *branch* and return its ID."""
        pipeline = self._project.pipelines.create({
            "ref": branch,
            "variables": [
                {"key": k, "value": v}
                for k, v in (variables or {}).items()
            ],
        })
        logger.info("Pipeline %d triggered on %s", pipeline.id, branch)
        return pipeline.id

    def get_pipeline_status(self, pipeline_id: int) -> PipelineResult:
        """Poll the current status of a pipeline."""
        pl = self._project.pipelines.get(pipeline_id)
        jobs = pl.jobs.list(get_all=True)
        return PipelineResult(
            pipeline_id=pl.id,
            status=pl.status,
            web_url=pl.web_url,
            jobs=[
                {
                    "name": j.name,
                    "stage": j.stage,
                    "status": j.status,
                }
                for j in jobs
            ],
        )
