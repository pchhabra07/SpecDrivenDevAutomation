# GitHub & GitHub Issues Automation Guide

This guide provides operational and architectural documentation for running the Spec-Driven Development Automation Pipeline on **GitHub** and **GitHub Issues** (branch `GitHub_Issues_Automation`).

---

## 1. Architecture Overview

The pipeline automates end-to-end software feature development driven by specifications and tests:

```
                  ┌──────────────────────┐
                  │ GitHub Issue Created │
                  │ (Label: SpecDrivenDev)│
                  └──────────┬───────────┘
                             │ (Webhook: /webhooks/github)
                             ▼
                  ┌──────────────────────┐
                  │ Stage 1: Triage      │
                  │ Eligibility Check    │
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │ Stage 2: Spec & Test │
                  │ LLM Generation       │
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │ Gate 1: Human Review │
                  │ (Draft PR on GitHub) │
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │ Stage 3: Code Gen    │
                  │ (Locked Test Checks) │
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │ Stage 4: Validation  │
                  │ (GitHub Actions CI)  │
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │ Gate 2 & Stage 5: PR │
                  │ Review & Auto-Merge  │
                  └──────────────────────┘
```

---

## 2. Configuration & Environment Variables

Configure the pipeline using environment variables or a `.env` file:

| Variable | Description | Default / Example | Required (GitHub) |
|---|---|---|---|
| `PLATFORM` | Target VCS and issue tracker platform (`github` or `gitlab`) | `github` | Yes |
| `GITHUB_TOKEN` | Personal Access Token (classic or fine-grained) | `ghp_...` | Yes |
| `GITHUB_REPO` | Default GitHub repository (`owner/repo`) | `my-org/my-service` | Yes |
| `GITHUB_BASE_URL` | GitHub API base URL (override for GitHub Enterprise) | `https://api.github.com` | No |
| `GITHUB_WEBHOOK_SECRET` | HMAC-SHA256 secret for validating webhooks | `your-github-webhook-secret` | Recommended |
| `GITHUB_AUDIT_REPO` | Designated repository for remote audit commits | `my-org/audit-log` | Optional |
| `GITHUB_WORKFLOW_FILE` | CI workflow filename in `.github/workflows/` | `spec-driven-dev-ci.yml` | No |
| `NIM_API_KEY` | NVIDIA NIM API key for LLM inference | `nvapi-...` | Yes |
| `QUALIFYING_LABEL` | Label required on issues to trigger pipeline | `SpecDrivenDev` | No |
| `MAX_CONCURRENT_TICKETS`| Maximum parallel worker pipelines | `2` | No |
| `GATE1_TIMEOUT_HOURS` | Maximum wait time for Gate 1 human review | `48.0` | No |
| `GATE2_TIMEOUT_HOURS` | Maximum wait time for Gate 2 human review | `48.0` | No |

---

## 3. GitHub Permissions & Webhook Setup

### Required Token Scopes
When generating a Personal Access Token (PAT) or configuring a GitHub App, provide:
- `repo` (Full repository access: commits, branches, pull requests, issues, labels)
- `workflow` (Trigger and monitor GitHub Actions workflows)
- `admin:repo_hook` (Read/write repository webhooks, if managing webhooks via API)

### Setting Up the Webhook in GitHub
1. Navigate to your repository on GitHub: **Settings > Webhooks > Add webhook**.
2. **Payload URL**: `https://<your-domain>/webhooks/github`
3. **Content type**: `application/json`
4. **Secret**: Enter the exact secret string defined in `GITHUB_WEBHOOK_SECRET`.
5. **Which events would you like to trigger this webhook?**:
   - Select **Let me select individual events**.
   - Check **Issues** (and optionally **Pull requests**).
6. Click **Add webhook**.

---

## 4. Issue Lifecycle & Acceptance Criteria

### Qualifying an Issue
An issue is picked up by the automation when it carries the qualifying label (`SpecDrivenDev` by default).

### Specifying Acceptance Criteria
The pipeline extracts acceptance criteria from the issue description using markdown headers or checklists:

```markdown
## Summary
Implement a secure idempotency key check on payment transactions.

## Acceptance Criteria
- [ ] Requests with duplicate `Idempotency-Key` headers within 24 hours must return the cached response.
- [ ] Requests without `Idempotency-Key` must proceed normally.
- [ ] Concurrent requests with the same key must return 409 Conflict.

## Technical Details
Use Redis key `idemp:{key}` with 24h TTL.
```

### Issue Transitions & Comments
- **On Qualification**: The pipeline adds `in-progress` label and posts an automation acknowledgment comment.
- **On Gate 1 (Spec & Test Ready)**: A comment is posted with a link to the draft Pull Request.
- **On Triage Disqualification**: The qualifying label (`SpecDrivenDev`) is automatically removed, and a comment explaining the rejection reasons is posted.
- **On Gate 2 (Code Ready)**: Label `under-review` is added, linking to the ready-for-review Pull Request.
- **On Final Merge**: The issue state is updated to `closed`, and audit logs are committed.

---

## 5. Pull Requests, Gates, and CI/CD

### Gate 1: Specification & Test Approval
1. The pipeline creates a branch `spec/<issue-id>` from `main`.
2. Stage 2 generates OpenAPI specs, Markdown specifications, and unit/contract test files.
3. A **Draft Pull Request** is opened on GitHub.
4. The reviewer inspects the specs and tests.
5. Approval can be given via:
   - GitHub Pull Request Review: Click **Review changes > Approve**.
   - Pull Request comment containing `LGTM` or `APPROVED`.
6. Once approved, the pipeline automatically detects approval and proceeds to Stage 3 (Code Generation).

### Stage 3: Code Generation & Test Locking
- The pipeline generates application code to satisfy the approved specs and tests.
- **Tamper Prevention**: The pipeline hashes all test files from Gate 1. If any test file is modified or deleted during code generation, the commit is rejected immediately, preventing AI models from "passing" tests by modifying assertions.
- Multi-file changes are committed atomically using the GitHub Git Data API (`create_git_tree`, `create_git_commit`, `edit_git_ref`).

### Stage 4: CI/CD Quality Gates
The pipeline triggers or listens to the GitHub Actions workflow (`.github/workflows/spec-driven-dev-ci.yml`) which runs 5 quality gates:
1. `lint`: Static analysis and formatting.
2. `security-sast`: SAST vulnerability scan (e.g. Semgrep).
3. `security-deps`: Dependency vulnerability scan (e.g. Trivy).
4. `test`: Automated test suite execution with code coverage enforcement.
5. `sonarqube`: SonarCloud / SonarQube quality gate verification.

### Gate 2: Pull Request Promotion & Auto-Merge
1. Upon CI passage, the Pull Request title is promoted from `Draft: ...` to ready for review.
2. Reviewers verify implementation diffs and CI results.
3. Reviewer submits review:
   - **Approve**: Pipeline merges the Pull Request (squash commit) and closes the GitHub Issue.
   - **Request Changes**: Pipeline feeds reviewer comments back into the code generation repair loop.

---

## 6. Running and Verifying Locally

### Running the Webhook Service
```bash
uvicorn webhook.app:app --host 0.0.0.0 --port 8000 --reload
```

### Checking Health & Metrics
- **Health check**: `curl http://localhost:8000/health`
- **Prometheus metrics**: `curl http://localhost:8000/metrics`

### Executing Full Test Suite
```bash
python -m pytest tests/unit/ -v
```
All 150 unit tests cover:
- GitHub API client operations (commits, branches, PRs, workflows).
- GitHub Issues client (acceptance criteria extraction, label transitions, lifecycle comments).
- Human Gate polling & review detection.
- Webhook signature verification and queue deduplication.
- Token bucket rate limiter, checkpointing, and tamper prevention.
