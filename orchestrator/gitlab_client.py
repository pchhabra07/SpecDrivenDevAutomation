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

from orchestrator.models import MergeRequestInfo, PipelineResult

logger = logging.getLogger(__name__)


@dataclass
class FileEntry:
    """A single file from the repository tree."""

    path: str
    type: str  # "blob" or "tree"


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

    def get_branch_diff(
        self,
        source_branch: str,
        target_branch: str | None = None,
        max_lines: int = 400,
    ) -> str:
        """
        Return a unified diff summary of *source_branch* vs *target_branch*.
        """
        if target_branch is None:
            target_branch = self.get_default_branch()
        try:
            compare = self._project.repository_compare(
                from_=target_branch,
                to=source_branch,
            )
            diff_parts: list[str] = []
            for d in compare.get("diffs", []):
                old_path = d.get("old_path", "")
                new_path = d.get("new_path", "")
                diff_text = d.get("diff", "")
                header = f"--- {old_path}\n+++ {new_path}"
                diff_parts.append(header)
                diff_parts.append(diff_text)
            full_diff = "\n".join(diff_parts)
            lines = full_diff.splitlines()
            if len(lines) > max_lines:
                lines = lines[:max_lines] + [
                    f"\n[... diff truncated at {max_lines} lines for token budget]"
                ]
            result = "\n".join(lines)
            logger.info(
                "Branch diff %s..%s: %d diff line(s) returned.",
                target_branch, source_branch, len(lines),
            )
            return result
        except Exception as exc:
            logger.warning("Could not fetch branch diff %s..%s: %s", target_branch, source_branch, exc)
            return ""

    def get_pr_diff(
        self,
        source_branch: str,
        target_branch: str | None = None,
        max_lines: int = 400,
    ) -> str:
        """Compatibility alias for get_branch_diff."""
        return self.get_branch_diff(source_branch, target_branch, max_lines)

    def get_default_branch(self) -> str:
        """Return the repository's default branch name."""
        return self._project.default_branch or "main"

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
            f = self._project.files.get(file_path=path, ref=branch)
            f.delete(branch=branch, commit_message=message)
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
        Enforces test-file locking via blocked_paths.
        """
        if not files:
            logger.warning("commit_files called with 0 files on branch %s; skipping commit.", branch)
            target_branch = self._project.branches.get(branch)
            return target_branch.commit["id"]

        if blocked_paths:
            violations = set(files.keys()) & blocked_paths
            if violations:
                raise ValueError(
                    f"Commit rejected — attempt to modify locked files: {violations}"
                )

        actions: list[dict[str, str]] = []
        for file_path, content in files.items():
            action = "update" if self._file_exists(file_path, branch) else "create"
            actions.append({
                "action": action,
                "file_path": file_path,
                "content": content,
            })

        commit = self._project.commits.create({
            "branch": branch,
            "commit_message": commit_message,
            "actions": actions,
        })
        logger.info(
            "Committed %d file(s) to %s (commit %s)",
            len(files), branch, commit.id[:8],
        )
        return commit.id

    def _file_exists(self, file_path: str, ref: str) -> bool:
        """Check whether a file already exists at *ref*."""
        try:
            self._project.files.get(file_path=file_path, ref=ref)
            return True
        except gitlab.exceptions.GitlabGetError:
            return False

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
        mr_title = f"Draft: {title}" if draft and not title.startswith("Draft:") else title
        try:
            mr = self._project.mergerequests.create({
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": mr_title,
                "description": description,
            })
        except gitlab.exceptions.GitlabCreateError as exc:
            if "already exists" in str(exc).lower():
                logger.info("MR already exists for %s; fetching existing MR", source_branch)
                mrs = self._project.mergerequests.list(
                    source_branch=source_branch,
                    target_branch=target_branch,
                    state="opened",
                )
                if mrs:
                    mr = mrs[0]
                    return MergeRequestInfo(
                        iid=mr.iid,
                        web_url=mr.web_url,
                        state=mr.state,
                        draft=getattr(mr, "draft", False) or mr.title.startswith("Draft:"),
                    )
            raise

        info = MergeRequestInfo(
            iid=mr.iid,
            web_url=mr.web_url,
            state=mr.state,
            draft=draft,
        )
        logger.info("MR !%d created: %s", info.iid, info.web_url)
        return info

    def create_pr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        draft: bool = True,
    ) -> MergeRequestInfo:
        """Compatibility alias for create_mr."""
        return self.create_mr(
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
            draft=draft,
        )

    def get_pr_url(self, mr_iid: int) -> str:
        """Return canonical URL for merge request."""
        try:
            mr = self._project.mergerequests.get(mr_iid)
            return mr.web_url
        except Exception:
            return f"{getattr(self._project, 'web_url', 'https://gitlab.com')}/-/merge_requests/{mr_iid}"

    def get_mr_approvals(self, mr_iid: int) -> dict[str, Any]:
        """Fetch approval status of an MR."""
        mr = self._project.mergerequests.get(mr_iid)
        approvals = mr.approvals.get()
        approved = getattr(approvals, "approved", False)
        return {
            "approved": bool(approved),
            "approved_by": [
                a["user"]["username"] for a in getattr(approvals, "approved_by", [])
            ],
            "changes_requested": False,
        }

    def get_pr_approvals(self, mr_iid: int) -> dict[str, Any]:
        """Compatibility alias for get_mr_approvals."""
        return self.get_mr_approvals(mr_iid)

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
            if not n.system
        ]

    def get_pr_notes(self, mr_iid: int) -> list[dict[str, Any]]:
        """Compatibility alias for get_mr_notes (S5-4)."""
        return self.get_mr_notes(mr_iid)

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

    def get_job_trace(self, job_id: int) -> str:
        """Fetch raw log output for a GitLab job by ID."""
        try:
            job = self._project.jobs.get(job_id)
            trace = job.trace()
            if isinstance(trace, bytes):
                return trace.decode("utf-8", errors="replace")
            return str(trace) if trace else ""
        except Exception as exc:
            logger.warning("Could not fetch trace for job %d: %s", job_id, exc)
            return ""

    def get_job_log(self, pipeline_id: int, job_name: str) -> str:
        """Fetch log for a specific job name in a pipeline."""
        try:
            pl = self._project.pipelines.get(pipeline_id)
            for j in pl.jobs.list(get_all=True):
                if j.name.lower() == job_name.lower() or job_name.lower() in j.name.lower():
                    return self.get_job_trace(j.id)
        except Exception as exc:
            logger.warning("Could not fetch log for job '%s' in pipeline %d: %s", job_name, pipeline_id, exc)
        return ""

