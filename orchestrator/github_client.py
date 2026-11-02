"""
GitHub API client — wraps ``PyGithub`` for all repository operations.

Replaces GitLabClient with native GitHub operations:
- File tree enumeration via Git Data API
- File reads with base64 decoding
- Branch creation and reference management
- Multi-file commits with locked test-file tamper enforcement
- Pull Request lifecycle (Draft PR creation, PR description update,
  ready-for-review promotion, label management, review approval polling,
  and feedback notes extraction)
- GitHub Actions workflow trigger and status monitoring
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from github import Auth, Github, GithubException, InputGitTreeElement

logger = logging.getLogger(__name__)


@dataclass
class FileEntry:
    """A single file from the repository tree."""

    path: str
    type: str  # "blob" or "tree"


@dataclass
class PullRequestInfo:
    """Essential metadata about a created or fetched Pull Request."""

    number: int
    web_url: str
    state: str
    draft: bool

    @property
    def iid(self) -> int:
        """Alias for number to maintain compatibility with downstream stages."""
        return self.number


@dataclass
class WorkflowRunResult:
    """Terminal or in-progress result of a GitHub Actions workflow run."""

    pipeline_id: int  # equivalent to workflow_run_id
    status: str       # "success", "failed", "canceled", "in_progress", etc.
    web_url: str
    jobs: list[dict[str, Any]] = field(default_factory=list)


class GitHubClient:
    """High-level GitHub API facade for repository and PR operations."""

    @staticmethod
    def _clean_repo(repo: str) -> str:
        clean = str(repo).strip()
        if "github.com" in clean:
            clean = clean.split("github.com", 1)[1].lstrip("/:").removesuffix(".git")
        return clean

    def __init__(
        self,
        token: str,
        repo: str,
        base_url: str = "https://api.github.com",
        workflow_file: str | None = None,
    ) -> None:
        self._token = token
        self._workflow_file = workflow_file
        if token:
            auth = Auth.Token(token)
            self._gh = Github(auth=auth, base_url=base_url)
        else:
            self._gh = Github(base_url=base_url)

        self._repo_name = self._clean_repo(repo)
        self._repo = self._gh.get_repo(self._repo_name)
        logger.info("GitHub client initialised for repo %s", self._repo.full_name)

    @property
    def repo_name(self) -> str:
        """Return full repository name ('owner/repo')."""
        return self._repo.full_name

    @property
    def short_repo_name(self) -> str:
        """Return short repository name (e.g. 'Full-Stack-To-Do-List')."""
        return self._repo_name.split("/")[-1].removesuffix(".git")

    @property
    def default_workflow_file(self) -> str:
        """Dynamic default workflow file: discovers any workflow in .github/workflows or falls back to repo name."""
        if self._workflow_file:
            return self._workflow_file
        try:
            tree = self.get_file_tree(ref=self.get_default_branch(), path=".github/workflows")
            for entry in tree:
                p = entry.path.replace("\\", "/")
                if p.endswith(".yml") or p.endswith(".yaml"):
                    import os
                    return os.path.basename(p)
        except Exception:
            pass
        from orchestrator.ci_generator import get_target_ci_filename
        return get_target_ci_filename(self._repo_name)

    @property
    def project_id(self) -> str:
        """Alias for repo_name to maintain compatibility with stages."""
        return self.repo_name

    def switch_repo(self, repo_name: str) -> None:
        """Switch active GitHub repository."""
        self._repo_name = self._clean_repo(repo_name)
        self._repo = self._gh.get_repo(self._repo_name)
        logger.info("GitHub client switched to repo %s", self._repo.full_name)

    def switch_project(self, repo_identifier: str | int) -> None:
        """Compatibility alias for switch_repo."""
        self.switch_repo(str(repo_identifier))

    # ── Repository reads ────────────────────────────────────────────────

    def get_file_tree(
        self,
        ref: str | None = None,
        path: str = "",
        recursive: bool = True,
    ) -> list[FileEntry]:
        """Return the full file tree for *ref* under *path*."""
        if ref is None:
            ref = self.get_default_branch()
        try:
            # Resolve branch name to commit SHA if needed
            branch = self._repo.get_branch(ref)
            sha = branch.commit.sha
        except GithubException:
            sha = ref

        git_tree = self._repo.get_git_tree(sha=sha, recursive=recursive)
        results: list[FileEntry] = []
        for item in git_tree.tree:
            if path and not item.path.startswith(path):
                continue
            results.append(FileEntry(path=item.path, type=item.type))
        return results

    def get_file_content(self, file_path: str, ref: str | None = None) -> str:
        """Return the decoded UTF-8 content of a single file."""
        if ref is None:
            ref = self.get_default_branch()
        content_file = self._repo.get_contents(file_path, ref=ref)
        if isinstance(content_file, list):
            raise ValueError(f"Path '{file_path}' is a directory, not a file")
        return content_file.decoded_content.decode("utf-8")

    def get_default_branch(self) -> str:
        """Return the name of the project's default branch."""
        return self._repo.default_branch

    def get_branch_diff(
        self,
        source_branch: str,
        target_branch: str | None = None,
        max_lines: int = 400,
    ) -> str:
        """
        Return a unified diff summary of *source_branch* vs *target_branch*.

        GAP-5 (PR diff injection): Gives the LLM a compact picture of what
        was already committed on the code branch before the current review
        cycle, so it does not repeat or regress prior work.

        Parameters
        ----------
        source_branch : str
            The feature / code branch (e.g. ``automation/TICKET-1/code``).
        target_branch : str | None
            The base branch to diff against.  Defaults to the repo's
            default branch.
        max_lines : int
            Cap on returned diff lines to stay within token budget.

        Returns
        -------
        str
            Unified diff text, truncated to *max_lines* lines, or ``""``
            on failure.
        """
        if target_branch is None:
            target_branch = self.get_default_branch()
        try:
            comparison = self._repo.compare(target_branch, source_branch)
            diff_lines: list[str] = []
            for f in comparison.files:
                diff_lines.append(f"--- {f.previous_filename or f.filename}")
                diff_lines.append(f"+++ {f.filename}")
                patch = getattr(f, "patch", None)
                if patch:
                    diff_lines.extend(patch.splitlines())
                diff_lines.append("")

            if len(diff_lines) > max_lines:
                diff_lines = diff_lines[:max_lines] + [
                    f"[... diff truncated at {max_lines} lines for token budget]"
                ]
            result = "\n".join(diff_lines)
            logger.info(
                "Branch diff %s..%s: %d diff line(s) returned.",
                target_branch, source_branch, len(diff_lines),
            )
            return result
        except Exception as exc:
            logger.warning(
                "Could not fetch branch diff %s..%s: %s", target_branch, source_branch, exc
            )
            return ""

    def get_pr_diff(
        self,
        source_branch: str,
        target_branch: str | None = None,
        max_lines: int = 400,
    ) -> str:
        """Compatibility alias for get_branch_diff."""
        return self.get_branch_diff(source_branch, target_branch, max_lines)

    # ── Branch management ───────────────────────────────────────────────

    def create_branch(self, branch_name: str, ref: str | None = None) -> None:
        """Create a new branch from *ref*."""
        if ref is None:
            ref = self.get_default_branch()
        base_branch = self._repo.get_branch(ref)
        base_sha = base_branch.commit.sha
        self._repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
        logger.info("Branch created: %s (from %s @ %s)", branch_name, ref, base_sha[:8])

    def branch_exists(self, branch_name: str) -> bool:
        """Check whether a branch already exists."""
        try:
            self._repo.get_branch(branch_name)
            return True
        except GithubException:
            return False

    def delete_file(self, path: str, branch: str, message: str) -> bool:
        """Delete a file from *branch* if it exists."""
        try:
            content = self._repo.get_contents(path, ref=branch)
            if not isinstance(content, list):
                self._repo.delete_file(path, message, content.sha, branch=branch)
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
            target_branch = self._repo.get_branch(branch)
            return target_branch.commit.sha

        if blocked_paths:
            violations = set(files.keys()) & blocked_paths
            if violations:
                raise ValueError(
                    f"Commit rejected — attempt to modify locked files: {violations}"
                )

        target_branch = self._repo.get_branch(branch)
        base_commit = target_branch.commit
        base_tree = base_commit.commit.tree

        tree_elements = [
            InputGitTreeElement(
                path=path,
                mode="100644",
                type="blob",
                content=content,
            )
            for path, content in files.items()
        ]

        new_tree = self._repo.create_git_tree(tree_elements, base_tree)
        new_commit = self._repo.create_git_commit(
            commit_message,
            new_tree,
            [base_commit.commit],
        )

        ref = self._repo.get_git_ref(f"heads/{branch}")
        ref.edit(new_commit.sha)

        logger.info("Commit %s on %s (%d files)", new_commit.sha[:10], branch, len(files))
        return new_commit.sha

    # ── Pull Requests ───────────────────────────────────────────────────

    def create_pr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        draft: bool = True,
    ) -> PullRequestInfo:
        """Create a new pull request (draft by default)."""
        pr_title = f"Draft: {title}" if draft and not title.startswith("Draft:") else title
        try:
            pr = self._repo.create_pull(
                title=pr_title,
                body=description,
                head=source_branch,
                base=target_branch,
                draft=draft,
            )
        except GithubException as exc:
            error_str = str(exc).lower()
            if "already exists" in error_str:
                logger.info("Pull request already exists for %s; fetching existing PR", source_branch)
                pulls = list(self._repo.get_pulls(state="open"))
                matched = [p for p in pulls if p.head.ref == source_branch]
                if matched:
                    pr = matched[0]
                    info = PullRequestInfo(
                        number=pr.number,
                        web_url=pr.html_url,
                        state=pr.state,
                        draft=getattr(pr, "draft", False),
                    )
                    logger.info("Found existing PR #%d: %s", info.number, info.web_url)
                    return info

            # Fallback for accounts/plans without native draft PRs
            logger.warning("Native draft PR failed (%s); creating standard PR with Draft title", exc)
            try:
                pr = self._repo.create_pull(
                    title=pr_title,
                    body=description,
                    head=source_branch,
                    base=target_branch,
                )
            except GithubException as fallback_exc:
                if "already exists" in str(fallback_exc).lower():
                    logger.info("Pull request already exists for %s; fetching existing PR", source_branch)
                    pulls = list(self._repo.get_pulls(state="open"))
                    matched = [p for p in pulls if p.head.ref == source_branch]
                    if matched:
                        pr = matched[0]
                        info = PullRequestInfo(
                            number=pr.number,
                            web_url=pr.html_url,
                            state=pr.state,
                            draft=getattr(pr, "draft", False),
                        )
                        logger.info("Found existing PR #%d: %s", info.number, info.web_url)
                        return info
                raise

        info = PullRequestInfo(
            number=pr.number,
            web_url=pr.html_url,
            state=pr.state,
            draft=draft,
        )
        logger.info("PR #%d created: %s", info.number, info.web_url)
        return info

    def create_mr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        draft: bool = True,
    ) -> PullRequestInfo:
        """Compatibility alias for create_pr."""
        return self.create_pr(
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
            draft=draft,
        )

    def get_pr_url(self, pr_number: int) -> str:
        """Return the canonical web URL for a pull request (S5-2)."""
        try:
            return f"{self._repo.html_url}/pull/{pr_number}"
        except Exception:
            logger.warning("Could not determine PR URL — returning generic placeholder.")
            return f"https://github.com/pull/{pr_number}"

    def get_pr_approvals(self, pr_number: int) -> dict[str, Any]:
        """
        Fetch approval status of a Pull Request (S5-3).
        Inspects formal review states ('APPROVED', 'CHANGES_REQUESTED') from
        GitHub Reviews API, tracking the latest review per author.
        Conversational issue comments are not treated as formal approvals/changes.
        """
        pr = self._repo.get_pull(pr_number)
        reviews = list(pr.get_reviews())

        # Track the latest decision per reviewer.
        # Note: COMMENTED reviews do not dismiss or override an earlier
        # APPROVED or CHANGES_REQUESTED review. DISMISSED clears the decision.
        latest_review_per_user: dict[str, str] = {}
        for r in reviews:
            state = (r.state or "").upper()
            author = r.user.login if r.user else "reviewer"
            if state in ("APPROVED", "CHANGES_REQUESTED"):
                latest_review_per_user[author] = state
            elif state == "DISMISSED":
                latest_review_per_user[author] = "DISMISSED"
            elif author not in latest_review_per_user:
                latest_review_per_user[author] = state

        approved_by: list[str] = []
        changes_requested_by: list[str] = []
        for author, state in latest_review_per_user.items():
            if state == "APPROVED":
                approved_by.append(author)
            elif state == "CHANGES_REQUESTED":
                changes_requested_by.append(author)

        return {
            "approved": len(approved_by) > 0,
            "approved_by": approved_by,
            "changes_requested": len(changes_requested_by) > 0,
            "changes_requested_by": changes_requested_by,
        }

    def get_mr_approvals(self, pr_number: int) -> dict[str, Any]:
        """Compatibility alias for get_pr_approvals."""
        return self.get_pr_approvals(pr_number)

    def update_pr_description(self, pr_number: int, description: str) -> None:
        """Update the body of an existing PR."""
        pr = self._repo.get_pull(pr_number)
        pr.edit(body=description)
        logger.info("PR #%d description updated", pr_number)

    def update_mr_description(self, pr_number: int, description: str) -> None:
        """Compatibility alias for update_pr_description."""
        self.update_pr_description(pr_number, description)

    def promote_pr_to_ready(self, pr_number: int) -> None:
        """Remove 'Draft:' prefix and mark PR ready for review."""
        pr = self._repo.get_pull(pr_number)
        new_title = pr.title
        if new_title.startswith("Draft: "):
            new_title = new_title.removeprefix("Draft: ")
        elif new_title.startswith("[Draft] "):
            new_title = new_title.removeprefix("[Draft] ")

        pr.edit(title=new_title)
        logger.info("PR #%d promoted to ready-for-review", pr_number)

    def promote_mr_to_ready(self, pr_number: int) -> None:
        """Compatibility alias for promote_pr_to_ready."""
        self.promote_pr_to_ready(pr_number)

    def add_pr_label(self, pr_number: int, label: str) -> None:
        """Add a label to a Pull Request."""
        issue = self._repo.get_issue(pr_number)
        issue.add_to_labels(label)
        logger.info("PR #%d labelled: %s", pr_number, label)

    def add_mr_label(self, pr_number: int, label: str) -> None:
        """Compatibility alias for add_pr_label."""
        self.add_pr_label(pr_number, label)

    def merge_pr(self, pr_number: int, squash: bool = True) -> None:
        """Merge a Pull Request (squash by default)."""
        pr = self._repo.get_pull(pr_number)
        pr.merge(merge_method="squash" if squash else "merge")
        logger.info("PR #%d merged (squash=%s)", pr_number, squash)

    def merge_mr(self, pr_number: int, squash: bool = True) -> None:
        """Compatibility alias for merge_pr."""
        self.merge_pr(pr_number, squash=squash)

    def get_pr_notes(self, pr_number: int) -> list[dict[str, Any]]:
        """Fetch all comments and reviews on a PR (for reviewer feedback routing)."""
        pr = self._repo.get_pull(pr_number)
        notes: list[dict[str, Any]] = []

        # Issue comments
        issue = self._repo.get_issue(pr_number)
        for c in issue.get_comments():
            author = c.user.login if c.user else "user"
            user_type = getattr(c.user, "type", "")
            is_bot = (
                user_type == "Bot"
                or author.endswith("[bot]")
                or author in ("github-actions", "codecov", "sonarcloud")
            )
            notes.append({
                "author": author,
                "body": c.body or "",
                "created_at": c.created_at,
                "system": is_bot,
            })

        # Review comments
        for r in pr.get_reviews():
            if r.body and r.body.strip():
                author = r.user.login if r.user else "reviewer"
                user_type = getattr(r.user, "type", "")
                is_bot = (
                    user_type == "Bot"
                    or author.endswith("[bot]")
                    or author in ("github-actions", "codecov", "sonarcloud")
                )
                notes.append({
                    "author": author,
                    "body": r.body,
                    "created_at": r.submitted_at,
                    "system": is_bot,
                })

        return notes

    def get_mr_notes(self, pr_number: int) -> list[dict[str, Any]]:
        """Compatibility alias for get_pr_notes."""
        return self.get_pr_notes(pr_number)

    def add_pr_comment(self, pr_number: int, comment: str) -> None:
        """Add a comment to a Pull Request."""
        issue = self._repo.get_issue(pr_number)
        issue.create_comment(comment)
        logger.info("Comment posted to PR #%d", pr_number)

    def add_mr_comment(self, pr_number: int, comment: str) -> None:
        """Compatibility alias for add_pr_comment."""
        self.add_pr_comment(pr_number, comment)

    # ── GitHub Actions CI/CD ───────────────────────────────────────────

    def trigger_workflow(
        self,
        branch: str,
        workflow_name_or_file: str | None = None,
        inputs: dict[str, str] | None = None,
        timeout_seconds: int = 35,
        commit_sha: str | None = None,
    ) -> int:
        """
        Trigger or find the active GitHub Actions workflow run on *branch*.
        """
        target_file = workflow_name_or_file or self.default_workflow_file

        # Check if push already triggered an active run for this commit
        if commit_sha and not inputs:
            try:
                runs = list(self._repo.get_workflow_runs(branch=branch))
                for run in runs[:5]:
                    if run.head_sha == commit_sha:
                        logger.info(
                            "Reusing workflow run %d triggered on push for commit %s on %s",
                            run.id,
                            commit_sha[:8],
                            branch,
                        )
                        return run.id
            except Exception:
                pass

        try:
            workflow = self._repo.get_workflow(target_file)
            workflow.create_dispatch(ref=branch, inputs=inputs or {})
            logger.info("Workflow dispatch created for %s on %s", target_file, branch)
        except Exception as exc:
            logger.debug(
                "Workflow dispatch not triggered for %s (%s); polling for push run",
                target_file,
                exc,
            )

        # Poll for active or latest workflow run on this branch
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout_seconds:
            try:
                runs = list(self._repo.get_workflow_runs(branch=branch))
                for run in runs[:5]:
                    if commit_sha and run.head_sha == commit_sha:
                        logger.info(
                            "Active workflow run %d found matching commit %s on %s (status: %s, workflow: %s)",
                            run.id,
                            commit_sha[:8],
                            branch,
                            run.status,
                            run.name,
                        )
                        return run.id
                    elif not commit_sha and run.status in ("queued", "in_progress"):
                        logger.info(
                            "Active workflow run %d found on %s (status: %s, workflow: %s)",
                            run.id,
                            branch,
                            run.status,
                            run.name,
                        )
                        return run.id
            except Exception as exc:
                logger.debug("Error checking workflow runs on %s: %s", branch, exc)
            time.sleep(3)

        # Fallback to most recent run on this branch
        runs = list(self._repo.get_workflow_runs(branch=branch))
        if runs:
            latest = runs[0]
            logger.info(
                "Returning latest workflow run %d on %s (head_sha: %s, status: %s)",
                latest.id,
                branch,
                latest.head_sha[:8],
                latest.status,
            )
            return latest.id

        logger.warning("No workflow runs found on branch %s for %s", branch, target_file)
        return 1

    def trigger_pipeline(
        self,
        branch: str,
        variables: dict[str, str] | None = None,
        commit_sha: str | None = None,
    ) -> int:
        """Compatibility alias for trigger_workflow."""
        return self.trigger_workflow(
            branch=branch, inputs=variables, commit_sha=commit_sha
        )

    def get_job_trace(self, job_id: int) -> str:
        """Fetch raw log output for a workflow job by ID."""
        import requests

        try:
            url = f"https://api.github.com/repos/{self.repo_name}/actions/jobs/{job_id}/logs"
            headers = {"Accept": "application/vnd.github+json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            res = requests.get(url, headers=headers, allow_redirects=True, timeout=30)
            if res.status_code == 200:
                return res.text
        except Exception as exc:
            logger.warning("Could not fetch logs for job %d: %s", job_id, exc)
        return ""

    def get_job_log(self, run_or_job_id: int, job_name: str | None = None) -> str:
        """Fetch log output for a workflow job by ID or by (run_id, job_name)."""
        if job_name is not None:
            try:
                run = self._repo.get_workflow_run(run_or_job_id)
                for job in run.jobs():
                    if job.name.lower() == job_name.lower() or job_name.lower() in job.name.lower():
                        return self.get_job_trace(job.id)
            except Exception as exc:
                logger.warning("Could not fetch log for job '%s' in run %d: %s", job_name, run_or_job_id, exc)
            return ""
        return self.get_job_trace(run_or_job_id)

    def get_workflow_status(self, run_id: int) -> WorkflowRunResult:
        """Poll the status of a GitHub Actions workflow run."""
        try:
            run = self._repo.get_workflow_run(run_id)
        except Exception as exc:
            logger.warning("Workflow run %d not found on GitHub: %s", run_id, exc)
            return WorkflowRunResult(
                pipeline_id=run_id,
                status="failed",
                web_url=f"{self._repo.html_url}/actions",
                jobs=[],
            )

        status = run.status
        conclusion = run.conclusion

        if status == "completed":
            mapped_status = "success" if conclusion == "success" else "failed"
        elif status in ("queued", "in_progress"):
            mapped_status = "running"
        else:
            mapped_status = conclusion or status or "unknown"

        jobs: list[dict[str, Any]] = []
        try:
            for j in run.jobs():
                job_status = (
                    "success"
                    if j.conclusion == "success"
                    else (j.conclusion or j.status)
                )
                jobs.append({
                    "id": j.id,
                    "name": j.name,
                    "status": job_status,
                    "conclusion": j.conclusion,
                    "html_url": j.html_url,
                })
        except Exception as exc:
            logger.warning("Could not fetch jobs for workflow run %d: %s", run_id, exc)

        return WorkflowRunResult(
            pipeline_id=run.id,
            status=mapped_status,
            web_url=run.html_url,
            jobs=jobs,
        )

    def get_pipeline_status(self, run_id: int) -> WorkflowRunResult:
        """Compatibility alias for get_workflow_status."""
        return self.get_workflow_status(run_id)
