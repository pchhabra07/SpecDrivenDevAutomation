# GitHub & GitHub Issues Migration Plan

This plan details the migration of the Spec-Driven Development Automation Pipeline from **GitLab + JIRA** to **GitHub + GitHub Issues** on branch `GitHub_Issues_Automation`.

## 1. Migration Scope & Objectives

- **Target Branch**: `GitHub_Issues_Automation`
- **Issue Tracker**: Transition from JIRA REST API (`atlassian-python-api`) to **GitHub Issues** API via `PyGithub`.
- **Source Control & Pull Requests**: Transition from GitLab API (`python-gitlab`) to **GitHub Repositories & Pull Requests** API via `PyGithub`.
- **CI/CD Quality Gates**: Transition from `.gitlab-ci.yml` pipeline triggers to **GitHub Actions** (`.github/workflows/spec-driven-dev-ci.yml`) workflow runs.
- **Webhook Ingestion**: Add GitHub Webhook receiver (`/webhooks/github`) with `X-Hub-Signature-256` HMAC authentication for `issues` events.
- **Audit Governance**: Transition remote audit log commits to a designated GitHub audit repository.
- **Preserve Pipeline Guarantees**: 5-stage workflow, test-locking enforcement, free-tier NIM rate limiting ($\le 32$ calls/min), sliding-window context pruning, checkpoint persistence, and multi-worker concurrency.

---

## 2. Technical Design

### A. GitHub Client (`orchestrator/github_client.py`)
Wraps `PyGithub` to implement:
- `get_file_tree(ref, path, recursive)`
- `get_file_content(file_path, ref)`
- `get_default_branch()`
- `create_branch(branch_name, ref)`
- `branch_exists(branch_name)`
- `commit_files(branch, files, commit_message, blocked_paths)`: Computes git trees and creates commits directly, enforcing test-file locking.
- `create_pr(source_branch, target_branch, title, description, draft=True)`: Creates draft pull request (aliased as `create_mr` for interface compatibility).
- `get_pr_approvals(pr_number)`: Inspects PR reviews (`APPROVED`, `CHANGES_REQUESTED`).
- `update_pr_description(pr_number, description)`
- `promote_pr_to_ready(pr_number)`
- `add_pr_label(pr_number, label)`
- `merge_pr(pr_number, squash=True)`
- `get_pr_comments(pr_number)`: Fetches PR review comments and issue comments.
- `trigger_workflow(branch, workflow_id, inputs)` / `get_workflow_status(run_id)`: Dispatches & inspects GitHub Actions runs.
- `switch_repo(repo_full_name)`: Dynamically targets another GitHub repository.

### B. GitHub Issues Client (`orchestrator/github_issue_client.py`)
Wraps `PyGithub` to implement:
- `get_ticket(issue_id)`: Retrieves GitHub Issue by number, extracts title, body, acceptance criteria (from `## Acceptance Criteria` section or markdown checklists), labels, assignees, and state.
- `post_comment(issue_id, body)`: Appends an issue comment.
- `transition_ticket(issue_id, transition_name)`: Updates issue status (closed/reopened) and labels.
- `remove_label(issue_id, label)`: Removes qualifying label (e.g. `SpecDrivenDev`) upon triage disqualification.
- `post_lifecycle_comment(issue_id, title, message, pr_url=None)`: Standardized markdown comment with link to GitHub PR.
- `has_qualifying_label(issue_id, label)`: Returns whether issue has the qualifying label.

### C. Human Approval Gate (`orchestrator/github_gate.py`)
- `wait_for_approval(github, pr_number, poll_interval_seconds, timeout_hours, return_on_changes_requested)`
- Polls GitHub PR reviews for state `"APPROVED"` (Gate 1 & Gate 2).
- Returns `CHANGES_REQUESTED` if reviewer requests changes on Gate 2, routing feedback into Stage 3.

### D. GitHub Actions CI Template (`.github/workflows/spec-driven-dev-ci.yml`)
Implements 5 gates:
1. `lint` (ESLint)
2. `security-sast` (Semgrep)
3. `security-deps` (Trivy)
4. `test` (Jest with coverage threshold)
5. `sonarqube` (SonarCloud / SonarQube)

### E. Webhook Ingestion (`webhook/app.py`)
- Endpoint: `/webhooks/github`
- Signature verification: `X-Hub-Signature-256` HMAC-SHA256.
- Ingests `issues` payloads with actions `opened`, `labeled`, `edited`.
- Dispatches qualifying issue numbers into the in-memory task queue for concurrent processing.

---

## 3. Implementation Steps

1. Update `requirements.txt` with `PyGithub>=2.3.0`.
2. Update `orchestrator/config.py` and `.env.example` with GitHub parameters.
3. Implement `orchestrator/github_client.py`.
4. Implement `orchestrator/github_issue_client.py`.
5. Implement `orchestrator/github_gate.py` and adapt `orchestrator/ci_poller.py`.
6. Create `.github/workflows/spec-driven-dev-ci.yml`.
7. Adapt `orchestrator/stages/` (Stages 1 through 5) and `orchestrator/main.py` for GitHub.
8. Adapt `orchestrator/checkpoint.py`, `orchestrator/notifier.py`, `orchestrator/repo_resolver.py`, and `orchestrator/audit_shipper.py`.
9. Update `webhook/app.py` with `/webhooks/github`.
10. Add unit tests in `tests/unit/` and verify 100% pass rate.
