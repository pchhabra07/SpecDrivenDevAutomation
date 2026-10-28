# SpecDrivenDevAutomation — Implementation Plan
## GitHub Issues Focused Pipeline: Bug Fixes & Cleanup

---

## Issue 1: `TicketData` is Coupled to `jira_client.py`

**Description**
The `TicketData` dataclass — the shared data model used across the entire pipeline — is defined inside `orchestrator/jira_client.py`. It is imported by `github_issue_client.py`, `stage1_context.py`, `repo_resolver.py`, and 10+ unit test files.

**Why it is happening**
The pipeline was originally built for JIRA. `TicketData` was defined alongside `JiraClient` and never extracted into a shared module. As GitHub Issues support was added, `github_issue_client.py` reused `TicketData` by importing it directly from the JIRA file rather than creating a clean shared model.

**How it needs to be fixed**
Create a new file `orchestrator/models.py` and move `TicketData` there:

```python
# orchestrator/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass
class TicketData:
    """Structured representation of a GitHub Issue for the pipeline."""
    ticket_id: str
    summary: str
    description: str
    acceptance_criteria: str
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    assignee: str | None = None
    reporter: str | None = None
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
```

Then replace every occurrence of:
```python
from orchestrator.jira_client import TicketData
from orchestrator.jira_client import JiraClient, TicketData
```
with:
```python
from orchestrator.models import TicketData
```

Files to update: `github_issue_client.py`, `stage1_context.py`, `repo_resolver.py`, and all `tests/unit/test_*.py` files that import `TicketData`. This must be done **before** any other step.

---

## Issue 2: Legacy JIRA and GitLab Files Still Exist in the Codebase

**Description**
Six files are either dead code or belong entirely to the JIRA/GitLab implementation that has been superseded. They continue to be present and create dependency confusion, inflate install requirements, and risk being accidentally imported.

**Why it is happening**
The codebase was migrated from a JIRA + GitLab architecture to GitHub Issues + GitHub Actions. The migration added new files (`github_client.py`, `github_issue_client.py`) but did not remove the old ones.

**How it needs to be fixed**
After completing Issue 1 (so `TicketData` is safely moved), delete these files:

```
orchestrator/jira_client.py           — replaced by github_issue_client.py + models.py
orchestrator/gitlab_client.py         — replaced by github_client.py
orchestrator/github_gate.py           — dead file, never imported by the pipeline
ci_templates/gitlab-ci-template.yml  — GitLab CI template, irrelevant for GitHub Actions
tests/unit/test_jira_client.py        — tests the deleted JIRA client
tests/unit/test_gitlab_gate.py        — tests the wrongly-named gate module (handled in Issue 3)
```

---

## Issue 3: Active Gate Module is Named `gitlab_gate.py`

**Description**
The file `orchestrator/gitlab_gate.py` is the **only active human gate module** used by `stage2_test_gen.py` and `stage5_pr.py`. It already handles both GitHub PRs and GitLab MRs via duck-typing (`hasattr` checks). Despite this, it is named `gitlab_gate.py`, which is misleading. A second file, `orchestrator/github_gate.py`, exists with the same functions but different parameter names (`client`/`pr_number` vs `gitlab`/`mr_iid`) and is **never imported anywhere in the pipeline** — it is dead code.

**Why it is happening**
`gitlab_gate.py` was written first for GitLab and then extended to support GitHub via duck-typing, but never renamed. `github_gate.py` was created as a GitHub-specific variant but the pipeline imports were never switched over to it, leaving it orphaned.

**How it needs to be fixed**
1. Rename `orchestrator/gitlab_gate.py` → `orchestrator/gate.py`
2. Delete `orchestrator/github_gate.py` (already covered in Issue 2)
3. Update both import lines:

In `orchestrator/stages/stage2_test_gen.py` line 37:
```python
# Before
from orchestrator.gitlab_gate import GateOutcome, check_approval_once, wait_for_approval
# After
from orchestrator.gate import GateOutcome, check_approval_once, wait_for_approval
```

In `orchestrator/stages/stage5_pr.py` lines 29–33:
```python
# Before
from orchestrator.gitlab_gate import (GateOutcome, GateResult, check_approval_once, wait_for_approval)
# After
from orchestrator.gate import (GateOutcome, GateResult, check_approval_once, wait_for_approval)
```

In `tests/unit/test_stage2_fixes.py`, remove the dead `github_gate` import block (lines 18–22) and update the `gitlab_gate` import:
```python
# Remove entirely:
from orchestrator.github_gate import (
    GateOutcome as GhGateOutcome,
    GateResult as GhGateResult,
    check_approval_once as gh_check_approval_once,
)
# Update:
from orchestrator.gate import (GateOutcome, GateResult, check_approval_once as gl_check_approval_once)
```

In `tests/unit/test_stage5_fixes.py` line 26 and `tests/unit/test_stage5_pr.py` line 11:
```python
from orchestrator.gate import GateOutcome, GateResult
```

Rename `tests/unit/test_gitlab_gate.py` → `tests/unit/test_gate.py` and update all patch targets inside from `orchestrator.gitlab_gate.*` → `orchestrator.gate.*`.

---

## Issue 4: `config.py` Still Defines JIRA and GitLab Settings

**Description**
`orchestrator/config.py` contains environment variable fields for `JIRA_BASE_URL`, `JIRA_API_TOKEN`, `JIRA_USER_EMAIL`, `JIRA_PROJECT_KEY`, `JIRA_WEBHOOK_SECRET`, `GITLAB_BASE_URL`, `GITLAB_API_TOKEN`, `GITLAB_REPO_ID`, `GITLAB_WEBHOOK_SECRET`, `AUDIT_GITLAB_REPO_ID`, and `PLATFORM`. The validator `validate_platform_credentials` still checks for incomplete JIRA or GitLab credentials and raises errors for them.

**Why it is happening**
Config was never trimmed after the GitHub-only migration. The `platform` field and legacy credential groups were kept as fallback options, but they now cause incorrect validation logic — the validator raises an error if `GITHUB_TOKEN` is absent but `JIRA+GitLab` credentials are complete, which is a path that no longer exists.

**How it needs to be fixed**
Remove these fields from `Settings`:
```
platform, jira_base_url, jira_api_token, jira_user_email, jira_project_key,
jira_webhook_secret, gitlab_base_url, gitlab_api_token, gitlab_repo_id,
gitlab_webhook_secret, audit_gitlab_repo_id
```

Replace `validate_platform_credentials` with a GitHub-only validator:
```python
@model_validator(mode="after")
def validate_github_credentials(self) -> Self:
    if self.github_repo:
        repo = self.github_repo.strip()
        if "github.com" in repo:
            repo = repo.split("github.com", 1)[1].lstrip("/:").removesuffix(".git")
        self.github_repo = repo
    if self.github_audit_repo:
        audit_repo = self.github_audit_repo.strip()
        if "github.com" in audit_repo:
            audit_repo = audit_repo.split("github.com", 1)[1].lstrip("/:").removesuffix(".git")
        self.github_audit_repo = audit_repo
    if not self.github_token or not self.github_repo:
        raise ValueError("GITHUB_TOKEN and GITHUB_REPO are required.")
    return self
```

---

## Issue 5: `main.py` Still Conditionally Instantiates `JiraClient` and `GitLabClient`

**Description**
`run_pipeline()` in `main.py` checks `settings.platform == "github"` and only then creates GitHub clients. The `else` branch instantiates `JiraClient` and `GitLabClient`. Both legacy client classes are also imported at the top of the file even though they will always be dead code after the `platform` field is removed.

**Why it is happening**
The `else` branch was the original pipeline path and was left in place when GitHub support was added as the primary path.

**How it needs to be fixed**
Remove the two legacy imports:
```python
# Remove:
from orchestrator.gitlab_client import GitLabClient
from orchestrator.jira_client import JiraClient
```

Remove the `if/else` and make the GitHub client instantiation unconditional:
```python
issue_client = GitHubIssueClient(
    token=settings.github_token,
    repo=settings.github_repo,
    base_url=settings.github_base_url,
)
vcs_client = GitHubClient(
    token=settings.github_token,
    repo=settings.github_repo,
    base_url=settings.github_base_url,
    workflow_file=settings.github_workflow_file or None,
)
```

Replace the variable names `jira` and `gitlab` throughout `run_pipeline()` with `issue_client` and `vcs_client` for clarity.

Also update `orchestrator/__init__.py`:
```python
# orchestrator — Automated GitHub Issue-to-PR Pipeline
# Spec-driven development: GitHub Issues → AI Coding Agent → GitHub PR
```

---

## Issue 6: Webhook Resume Re-Triggers Test Generation (Race Condition at Gate 1)

**Description**
In non-blocking (server) mode, when a reviewer submits a GitHub PR review, the webhook fires and re-enqueues the ticket. `run_pipeline()` reloads the checkpoint, sees `current_stage = 2` and `gate1_approved = False`, and re-enters Stage 2. Stage 2 then re-generates tests and re-commits them to the tests branch, overwriting whatever was already there — even if the reviewer just approved.

**Why it is happening**
When a `GatePendingError` is raised in Stage 2, the checkpoint saves `current_stage = 2`. There is no short-circuit in `main.py` that distinguishes "Stage 2 is running for the first time" from "Stage 2 is suspended at a gate waiting for a human response." On resume, `main.py` calls Stage 2 again from the top rather than jumping directly to the gate check.

**How it needs to be fixed**
Add a `gate_waiting` short-circuit at the top of `run_pipeline()`, right after the checkpoint is loaded, before Stage 1 runs:

```python
if checkpoint.gate_waiting == "gate1":
    checkpoint.current_stage = 2
    logger.info(
        "Resuming at Gate 1 checkpoint (PR #%d)",
        checkpoint.gate_waiting_mr_iid,
    )
elif checkpoint.gate_waiting == "gate2":
    checkpoint.current_stage = 5
    logger.info(
        "Resuming at Gate 2 checkpoint (PR #%d)",
        checkpoint.gate_waiting_mr_iid,
    )
```

This ensures that when the webhook re-enqueues a ticket, the pipeline skips directly to the gate check rather than re-running generation.

---

## Issue 7: Non-Blocking Gate Timeout Never Triggers Escalation

**Description**
In blocking mode, `wait_for_approval()` raises `GateTimeoutError` after `gate_timeout_hours`, which is caught and handled as a `"timeout"` status. In non-blocking (server/webhook) mode, `_stale_gate_monitor` in `webhook/app.py` detects the timeout and re-enqueues the ticket — but `run_pipeline()` then resumes, hits `check_approval_once()`, gets `PENDING` again, raises `GatePendingError`, and parks indefinitely. The escalation path in `main.py` (`stage5_result.status == "timeout"`) is never reached in non-blocking mode.

**Why it is happening**
The timeout escalation logic exists only inside the blocking gate polling loop. The non-blocking path has no equivalent check when it resumes from a stale checkpoint.

**How it needs to be fixed**
Add a timeout check at the top of `run_pipeline()`, immediately after the `gate_waiting` short-circuit (Issue 6):

```python
import time as _time

if checkpoint.gate_waiting and checkpoint.gate_waiting_since:
    elapsed_hours = (_time.time() - checkpoint.gate_waiting_since) / 3600
    if elapsed_hours > settings.gate_timeout_hours:
        logger.error(
            "Gate '%s' for ticket %s timed out after %.1fh.",
            checkpoint.gate_waiting, ticket_id, elapsed_hours,
        )
        issue_client.post_lifecycle_comment(
            ticket_id=ticket_id,
            title="Gate Timeout",
            message=(
                f"Gate '{checkpoint.gate_waiting}' timed out after "
                f"{settings.gate_timeout_hours}h with no human review. "
                "Pipeline halted. Please review manually."
            ),
        )
        return 1
```

---

## Issue 8: JIRA Webhook HMAC Validation Uses Wrong Header Alias

**Description**
`webhook/app.py` defines the JIRA webhook handler with the header alias `X-Hub-Signature`. Modern GitHub and JIRA webhooks send the signature as `X-Hub-Signature-256` (SHA-256). The handler reads `None` for every real webhook call because the header name does not match, then silently bypasses authentication because `hmac.compare_digest` is never reached when `signature_header` is `None` and the early-return path evaluates `not secret` — but if `jira_webhook_secret` **is** set, the handler returns `401` on every legitimate webhook.

**Why it is happening**
The handler was written using the older SHA-1 header name (`X-Hub-Signature`) which GitHub deprecated. The JIRA webhook format was also not verified against current JIRA Cloud documentation, which uses `X-Hub-Signature` with SHA-256 content.

**How it needs to be fixed**
Update the header alias in `handle_jira_webhook`:
```python
# Before:
x_hub_signature: str | None = Header(None, alias="X-Hub-Signature")
# After:
x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256")
```

And update the HMAC check to use the corrected variable:
```python
sig = x_hub_signature_256 or secret
if settings.jira_webhook_secret and not verify_jira_hmac(raw_body, sig, settings.jira_webhook_secret):
```

---

## Issue 9: `/webhooks/gitlab` Endpoint Still Exists in Webhook Server

**Description**
`webhook/app.py` defines a `POST /webhooks/gitlab` endpoint that receives GitLab MR and note events, looks up the matching ticket by PR number, and re-enqueues it. This endpoint validates against `gitlab_webhook_secret` from config (which is being removed in Issue 4) and will crash at startup once that config field is gone.

**Why it is happening**
The endpoint was part of the original GitLab pipeline and was not removed when the GitHub migration happened.

**How it needs to be fixed**
Delete the entire `handle_gitlab_webhook` function and its `@app.post("/webhooks/gitlab")` decorator from `webhook/app.py`. Also remove the `gitlab_webhook_secret` reference which will no longer exist in `Settings`.

---

## Issue 10: Gate 1 PR is Created as `draft=True`, Blocking GitHub Approvals

**Description**
In `stage2_test_gen.py`, the Gate 1 PR (the tests-for-review branch) is opened with `draft=True`. On GitHub, **draft pull requests cannot receive formal reviews** — the "Approve" and "Request Changes" options in the GitHub UI are greyed out. The gate polling loop (`check_approval_once` / `wait_for_approval`) checks `get_pr_approvals()` which inspects formal review states. A draft PR will therefore never transition from `PENDING` to `APPROVED`, and the pipeline hangs at Gate 1 indefinitely.

**Why it is happening**
The `draft=True` flag was appropriate for Stage 3's code-generation PR (where code is actively being iterated). It was incorrectly applied to the Gate 1 PR as well, which is a finished artifact (the generated test suite) ready for immediate human review.

**How it needs to be fixed**
In `stage2_test_gen.py`, change the Gate 1 `create_mr` call:
```python
# Before:
mr_info = gitlab.create_mr(
    source_branch=tests_branch,
    target_branch=context.default_branch,
    title=f"[Gate 1] Tests for {ticket.ticket_id}: {ticket.summary}",
    description=_build_mr_description(...),
    draft=True,
)
# After:
mr_info = gitlab.create_mr(
    source_branch=tests_branch,
    target_branch=context.default_branch,
    title=f"[Gate 1] Tests for {ticket.ticket_id}: {ticket.summary}",
    description=_build_mr_description(...),
    draft=False,
)
```

---

## Issue 11: Stage 3 Re-Hydration Misses New Files During Reviewer Feedback Cycles

**Description**
When Stage 5 returns `changes_requested` and the pipeline loops back to Stage 3, the code re-hydrates `current_sources` by iterating over `list(current_sources.keys())`. This only refreshes files that were already in `context.source_files` from Stage 1. If the reviewer's feedback references a file that was **newly created** during a previous Stage 3 iteration (e.g., a new utility module the LLM added), that file exists on `code_branch` but is not in `current_sources.keys()` — so it is never fetched. The LLM then generates code that conflicts with or duplicates the existing new file.

**Why it is happening**
The re-hydration loop was written to refresh known files, not to discover new ones. `checkpoint.modified_files` tracks all files ever committed to `code_branch`, including newly created ones, but this list is not used during re-hydration.

**How it needs to be fixed**
After the existing re-hydration loop in `stage3_code_gen.py`, add a pass over `checkpoint.modified_files`:

```python
# After the existing re-hydration loop:
for path in checkpoint.modified_files:
    if path not in current_sources and path not in locked_paths:
        try:
            content = vcs_client.get_file_content(path, ref=code_branch)
            current_sources[path] = content
            context.source_files[path] = content
        except Exception:
            pass
```

---

## Issue 12: `checkpoint.save()` Called Without Explicit Directory in Stage 3

**Description**
In `stage3_code_gen.py`, `checkpoint.save()` is called multiple times without passing a directory argument. The `save()` method uses `self.checkpoint_dir` as a fallback, which defaults to the dataclass field default of `".checkpoints"`. If the pipeline was invoked with a custom `checkpoint_dir` setting and the checkpoint was freshly instantiated (not loaded from disk), `self.checkpoint_dir` may still be the default `".checkpoints"` rather than the configured path, silently writing to the wrong location.

**Why it is happening**
Other call sites (Stage 2, `main.py`) pass `checkpoint_dir` explicitly to `checkpoint.save()`. Stage 3 was written using the no-argument form and relies on the instance field being correct, which is fragile.

**How it needs to be fixed**
Replace all bare `checkpoint.save()` calls in `stage3_code_gen.py` with:
```python
checkpoint.save(checkpoint.checkpoint_dir)
```

---

## Issue 13: `TEST_ONLY` CI Variable is Silently Ignored on Push-Triggered Workflow Runs

**Description**
Stage 3 calls `trigger_ci(gitlab, code_branch, variables={"TEST_ONLY": "true"})` intending to run only the test job and skip lint, SAST, dependency scanning, and SonarQube. In `github_client.py`, `trigger_workflow()` first checks if the push to `code_branch` already triggered a workflow run matching `commit_sha`. If it did, it **reuses that run** rather than dispatching a new one. Push-triggered runs do not carry `workflow_dispatch` inputs, so `github.event.inputs.test_only` is always empty string (`""`). The `!= 'true'` condition in the CI YAML evaluates to `true`, so all gates run on every Stage 3 iteration — doubling CI time and causing false Stage 4 gate failures from partial pipeline runs.

**Why it is happening**
GitHub Actions does not expose `workflow_dispatch` inputs on push-triggered runs. The CI YAML was written assuming all programmatic triggers go through `workflow_dispatch`, but the orchestrator reuses push-triggered runs as a performance optimization.

**How it needs to be fixed**
Add a workflow-level `env` block that normalizes the input with a default, and update all gate conditions to check `env.TEST_ONLY` instead of `github.event.inputs.test_only`:

```yaml
# At workflow level, add:
env:
  TEST_ONLY: ${{ github.event.inputs.test_only || 'false' }}

# Replace all gate conditions from:
if: ${{ github.event.inputs.test_only != 'true' }}
# To:
if: ${{ env.TEST_ONLY != 'true' }}
```

---

## Issue 14: `_attempt_stage4_repair` Sends Wrong Files to LLM When Failure Is in a Non-Top-10 File

**Description**
The Stage 4 repair prompt in `main.py` includes at most 10 source files (`list(editable_sources.items())[:10]`). The 10-file cap exists to control token usage, but the files are selected in dict iteration order — not by relevance to the failure. Gate logs from ESLint, Semgrep, or Trivy almost always name the specific file with the error. If that file happens to be at position 11+ in `editable_sources`, the LLM never sees it and produces a repair that targets unrelated files, which either fails to apply or introduces new issues.

**Why it is happening**
The file ordering was not made failure-aware when the Stage 4 repair loop was added. Dict iteration order is insertion order (Python 3.7+), which reflects the order Stage 1 fetched files — typically alphabetical by path, not by failure relevance.

**How it needs to be fixed**
Parse gate log output for referenced file paths and move those files to the front before applying the 10-file cap:

```python
import re as _re

mentioned_in_logs: set[str] = set()
for log_text in gate_logs.values():
    for match in _re.finditer(
        r'[\w./\-]+\.(?:js|ts|tsx|jsx|py|css|html|json)\b', log_text
    ):
        mentioned_in_logs.add(match.group(0).lstrip("./"))

prioritized = [p for p in editable_sources if any(m in p for m in mentioned_in_logs)]
rest = [p for p in editable_sources if p not in set(prioritized)]
ordered_sources = {p: editable_sources[p] for p in (prioritized + rest)[:10]}
# Use ordered_sources instead of list(editable_sources.items())[:10]
```

---

## Issue 15: `notifier.py` `_platform_names()` Returns "GitLab" / "Merge Request"

**Description**
`notifier.py` has a `_platform_names()` function that checks whether a URL contains `"gitlab"` and returns `("GitLab", "Merge Request")`. Otherwise it returns `("GitHub", "Pull Request")`. Since the pipeline is now GitHub-only, the GitLab branch is dead code and will never be reached — but it remains as noise and a potential source of incorrect notification messages if URL matching ever misbehaves.

**Why it is happening**
Left over from the original dual-platform support.

**How it needs to be fixed**
Simplify the function to always return GitHub terminology:
```python
def _platform_names(url: str) -> tuple[str, str]:
    return "GitHub", "Pull Request"
```

---

## Issue 16: `audit_shipper.py` Contains a GitLab Fallback Audit Path

**Description**
`audit_shipper.py` accepts a `gitlab` parameter and contains a fallback code path that attempts to commit the audit log to a GitLab repository (`audit_gitlab_repo_id`). Since `audit_gitlab_repo_id` is being removed from `config.py` (Issue 4), this path will raise an `AttributeError` at runtime if it is ever reached.

**Why it is happening**
The audit shipper was built to support both GitHub and GitLab as audit log destinations. The GitLab path was never removed during migration.

**How it needs to be fixed**
Remove the GitLab fallback block from `ship_audit_log()` and the `_commit_audit_to_git()` helper. Keep only the GitHub audit repo (`github_audit_repo`) logic. The `gitlab` parameter can remain typed as `Any` since it receives the `vcs_client` — just remove the dead GitLab-specific branches within the function.

---

## Issue 17: `protocols.py` `VCSClient` Declares `get_mr_approvals` but `GitHubClient` Implements `get_pr_approvals`

**Description**
The `VCSClient` protocol in `protocols.py` specifies `get_mr_approvals(self, mr_iid: int)` as a required method. `GitHubClient` does not implement this — it implements `get_pr_approvals(pr_number: int)`. The duck-typing in `gate.py` works around this via `hasattr` checks, but the protocol is misleading: any type checker running `mypy` or `pyright` on the codebase will flag `GitHubClient` as not satisfying `VCSClient`.

**Why it is happening**
`protocols.py` was written to match the GitLab client's naming convention and was never updated when `GitHubClient` was added with its own naming.

**How it needs to be fixed**
Update `VCSClient` to match the GitHub client's actual interface:
```python
# Before:
def get_mr_approvals(self, mr_iid: int) -> dict[str, Any]: ...
# After:
def get_pr_approvals(self, pr_number: int) -> dict[str, Any]: ...
```

---

## Issue 18: `requirements.txt` Contains Unused JIRA and GitLab Dependencies

**Description**
`requirements.txt` includes `python-gitlab>=4.5` and `atlassian-python-api>=3.41`. After removing `gitlab_client.py` and `jira_client.py`, nothing in the codebase imports from these packages. They still get installed in every environment and Docker image, adding unnecessary install time, attack surface, and image size.

**Why it is happening**
Dependencies were never pruned after the clients that used them were superseded.

**How it needs to be fixed**
Remove both lines from `requirements.txt`:
```
python-gitlab>=4.5          ← DELETE
atlassian-python-api>=3.41  ← DELETE
```

---

## Issue 19: Unit Tests Mock Against `JiraClient` Interface Instead of `GitHubIssueClient`

**Description**
`tests/unit/test_stage5_fixes.py` and `tests/unit/test_stage5_pr.py` create issue-client mocks using `MagicMock(spec=JiraClient)`. Once `jira_client.py` is deleted (Issue 2), these imports will raise `ModuleNotFoundError` and all tests in those files will fail to collect.

**Why it is happening**
The tests were written when `JiraClient` was the only issue tracker client. When `GitHubIssueClient` was added, the mock specs were not updated.

**How it needs to be fixed**
In both files, replace:
```python
from orchestrator.jira_client import JiraClient, TicketData
...
jira = MagicMock(spec=JiraClient)
```
with:
```python
from orchestrator.github_issue_client import GitHubIssueClient
from orchestrator.models import TicketData
...
issue_client = MagicMock(spec=GitHubIssueClient)
```
Update all references from `jira.` to `issue_client.` within those test functions.

---

## Issue 20: `.env.example` Still Documents JIRA and GitLab Variables

**Description**
`.env.example` contains sections for `JIRA_BASE_URL`, `JIRA_API_TOKEN`, `JIRA_USER_EMAIL`, `JIRA_PROJECT_KEY`, `GITLAB_BASE_URL`, `GITLAB_API_TOKEN`, `GITLAB_REPO_ID`, `JIRA_WEBHOOK_SECRET`, `GITLAB_WEBHOOK_SECRET`, `AUDIT_GITLAB_REPO_ID`, and `PLATFORM`. These no longer correspond to any field in `Settings` after Issue 4 is applied. A developer setting up the pipeline from this file will set variables that have no effect, and will not know which variables are actually required.

**Why it is happening**
`.env.example` was never updated to reflect the GitHub-only migration.

**How it needs to be fixed**
Replace `.env.example` entirely with a clean GitHub-focused version:

```env
# ── GitHub ────────────────────────────────────────────────────────────
GITHUB_TOKEN=ghp_your_personal_access_token
GITHUB_REPO=owner/repo-name
GITHUB_WEBHOOK_SECRET=your-webhook-hmac-secret
GITHUB_AUDIT_REPO=owner/audit-log-repo        # optional
GITHUB_WORKFLOW_FILE=                           # optional, defaults to <repo-name>-ci.yml

# ── NIM / LLM ─────────────────────────────────────────────────────────
NIM_API_KEY=your-nim-api-key
NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_MODEL=z-ai/glm-5.3
NIM_FALLBACK_MODEL=deepseek-ai/deepseek-v4-pro

# ── Pipeline Behaviour ────────────────────────────────────────────────
QUALIFYING_LABEL=SpecDrivenDev
MAX_CODE_ITERATIONS=6
GATE_TIMEOUT_HOURS=48
BLOCKING_GATES=true

# ── Notifications ─────────────────────────────────────────────────────
SLACK_WEBHOOK_URL=
ESCALATION_EMAIL=
```

---

## Execution Order

| Order | Issue | File(s) Changed |
|---|---|---|
| 1 | Issue 1 — Create `models.py`, migrate `TicketData` | `orchestrator/models.py` (new), 12+ files updated |
| 2 | Issue 2 — Delete 6 legacy files | `jira_client.py`, `gitlab_client.py`, `github_gate.py`, `gitlab-ci-template.yml`, `test_jira_client.py`, `test_gitlab_gate.py` |
| 3 | Issue 3 — Rename `gitlab_gate.py` → `gate.py`, update imports | `gate.py`, `stage2_test_gen.py`, `stage5_pr.py`, `test_stage2_fixes.py`, `test_stage5_fixes.py`, `test_stage5_pr.py`, `test_gate.py` |
| 4 | Issue 4 — Strip JIRA/GitLab fields from `config.py` | `orchestrator/config.py` |
| 5 | Issue 5 — Remove legacy client init from `main.py` | `orchestrator/main.py`, `orchestrator/__init__.py` |
| 6 | Issue 6 — Add `gate_waiting` short-circuit in `main.py` | `orchestrator/main.py` |
| 7 | Issue 7 — Add non-blocking gate timeout escalation in `main.py` | `orchestrator/main.py` |
| 8 | Issue 8 — Fix HMAC header alias in `webhook/app.py` | `webhook/app.py` |
| 9 | Issue 9 — Remove `/webhooks/gitlab` endpoint | `webhook/app.py` |
| 10 | Issue 10 — Set Gate 1 PR `draft=False` | `orchestrator/stages/stage2_test_gen.py` |
| 11 | Issue 11 — Fix Stage 3 new-file re-hydration | `orchestrator/stages/stage3_code_gen.py` |
| 12 | Issue 12 — Fix `checkpoint.save()` missing dir arg | `orchestrator/stages/stage3_code_gen.py` |
| 13 | Issue 13 — Fix `TEST_ONLY` CI variable on push runs | `.github/workflows/spec-driven-dev-ci.yml` |
| 14 | Issue 14 — Prioritize failing files in Stage 4 repair | `orchestrator/main.py` |
| 15 | Issue 15 — Simplify `_platform_names()` | `orchestrator/notifier.py` |
| 16 | Issue 16 — Remove GitLab path from `audit_shipper.py` | `orchestrator/audit_shipper.py` |
| 17 | Issue 17 — Fix `VCSClient` protocol method name | `orchestrator/protocols.py` |
| 18 | Issue 18 — Remove unused deps from `requirements.txt` | `requirements.txt` |
| 19 | Issue 19 — Update unit test mock specs | `tests/unit/test_stage5_fixes.py`, `tests/unit/test_stage5_pr.py` |
| 20 | Issue 20 — Rewrite `.env.example` | `.env.example` |
