# Spec-Driven Development Pipeline — How to Use

> **Automated AI-powered spec-to-code pipeline driven by GitHub Issues and GitHub Actions.**

---

## Table of Contents

1. [What This Pipeline Does](#what-this-pipeline-does)
2. [Architecture Overview](#architecture-overview)
3. [Prerequisites](#prerequisites)
4. [Quick Start (Single Repo)](#quick-start-single-repo)
5. [Multi-Repo Setup (Target Repo Workflow)](#multi-repo-setup-target-repo-workflow)
6. [Environment Variables Reference](#environment-variables-reference)
7. [Raising an Issue to Trigger the Pipeline](#raising-an-issue-to-trigger-the-pipeline)
8. [Pipeline Stages Explained](#pipeline-stages-explained)
9. [Human Gates (Approval Points)](#human-gates-approval-points)
10. [Webhook Server](#webhook-server)
11. [Monitoring & Health Checks](#monitoring--health-checks)
12. [Troubleshooting](#troubleshooting)

---

## What This Pipeline Does

The Spec-Driven Development Pipeline takes a **GitHub Issue** (a ticket describing a feature or bug) and **automatically**:

1. Reads the issue description and acceptance criteria.
2. Selects relevant source files from the repository using BM25 search, semantic ranking, and import-graph tracing.
3. Generates test cases that cover the acceptance criteria (Stage 2).
4. Opens a **Gate 1 PR** for human review of the tests.
5. Once approved, generates source code changes that make the tests pass (Stage 3).
6. Runs the full CI pipeline — ESLint, Semgrep SAST, Trivy SCA, Jest/Pytest, SonarQube (Stage 4).
7. Promotes the PR to ready-for-review and waits for a **Gate 2 human approval** (Stage 5).
8. On approval, the PR is merged and the issue is closed.

Only **one pipeline run executes at a time**. Additional triggers are queued and processed serially.

---

## Architecture Overview

```
GitHub Issue  ──►  Webhook Server  ──►  Orchestrator Pipeline
     (labeled)      /webhooks/github        Stage 1 → 2 → 3 → 4 → 5
                         │
              ┌──────────┴──────────┐
              │   Stage 2 Gate 1   │   ← Human reviews test PR
              │   Stage 5 Gate 2   │   ← Human approves code PR
              └─────────────────────┘
```

- **Webhook Server** — FastAPI service that receives GitHub webhook events, validates HMAC signatures, deduplicates, and enqueues issue IDs.
- **Orchestrator** — Python pipeline that runs all 5 stages serially. Checkpoints state to disk so it can resume after a crash or restart.
- **LLM Backend** — NVIDIA NIM API (configurable model, with fallback).
- **VCS** — All code commits, branches, and PRs are created on GitHub via PyGithub.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python 3.11+ | Tested on Python 3.13 |
| GitHub Personal Access Token (PAT) | Scopes: `repo`, `workflow`, `read:org` |
| NVIDIA NIM API Key | From [build.nvidia.com](https://build.nvidia.com) |
| A GitHub repository | Where issues will be raised and code will be committed |
| A server / VM with a public URL | To host the webhook server (or use `ngrok` for local dev) |

---

## Quick Start (Single Repo)

### 1. Clone and Install

```bash
git clone https://github.com/your-org/SpecDrivenDevAutomation.git
cd SpecDrivenDevAutomation
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```env
# Required
GITHUB_TOKEN=ghp_your_personal_access_token
GITHUB_REPO=owner/repo-name                  # The repo where issues are raised
GITHUB_WEBHOOK_SECRET=your-hmac-secret       # Any random string, used to verify webhook payloads

# LLM
NIM_API_KEY=your-nim-api-key
NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_MODEL=z-ai/glm-5.3

# Pipeline
QUALIFYING_LABEL=SpecDrivenDev               # Only issues with this label are processed
MAX_CONCURRENT_TICKETS=1                     # IMPORTANT: keep at 1 for serial execution
```

> **Keep `MAX_CONCURRENT_TICKETS=1`** — the pipeline is designed for serial execution. Running multiple instances concurrently against the same repository will cause race conditions on branches and PRs.

### 3. Start the Webhook Server

```bash
uvicorn webhook.app:app --host 0.0.0.0 --port 8000
```

The server exposes:
- `POST /webhooks/github` — receives GitHub webhook events
- `GET /health` — liveness probe
- `GET /metrics` — Prometheus-compatible metrics

### 4. Configure GitHub Webhooks on Your Repo

1. In your GitHub repository, go to **Settings → Webhooks → Add webhook**.
2. Set:
   - **Payload URL:** `https://your-server.example.com/webhooks/github`
   - **Content type:** `application/json`
   - **Secret:** the same value as `GITHUB_WEBHOOK_SECRET`
   - **Events:** Select **Let me select individual events** → check:
     - ✅ Issues
     - ✅ Pull request reviews
     - ✅ Issue comments
3. Click **Add webhook**.

### 5. Add the CI Workflow to Your Repo

Copy `.github/workflows/spec-driven-dev-ci.yml` to the **same path** in your target repository. This is the validation pipeline (ESLint, Semgrep, Trivy, tests) that Stage 4 triggers and monitors.

### 6. Trigger the Pipeline

Create a GitHub Issue in the repository with:
- A clear title and description
- The **`SpecDrivenDev`** label attached

The pipeline will start automatically within seconds.

---

## Multi-Repo Setup (Target Repo Workflow)

Use this approach when the **pipeline server** lives in one repository but you want it to process issues from **multiple other repositories**.

### How It Works

Each target repository gets a lightweight GitHub Actions workflow (provided in the `Target Repo Workflow/` folder). When a new issue is opened or labeled in the target repo, the workflow fires and sends a POST request to the pipeline server. The pipeline server processes the issue against the target repo.

### Setup Steps

#### Step 1 — Add the Workflow to Each Target Repo

Copy the file `Target Repo Workflow/.github/workflows/notify-spec-driven-pipeline.yml` into the target repository at the **exact same path**:

```
your-target-repo/
└── .github/
    └── workflows/
        └── notify-spec-driven-pipeline.yml
```

Commit and push it to the default branch.

#### Step 2 — Add Repository Secrets to Each Target Repo

In each target repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret Name | Value |
|---|---|
| `SPEC_PIPELINE_SERVER_URL` | The public URL of your pipeline server, e.g. `https://pipeline.example.com` |
| `SPEC_PIPELINE_WEBHOOK_SECRET` | The same secret as `GITHUB_WEBHOOK_SECRET` in your pipeline `.env` |

#### Step 3 — Configure `GITHUB_REPO` in the Pipeline

In the pipeline server's `.env`, set `GITHUB_REPO` to the **target repo** you want the pipeline to commit code to:

```env
GITHUB_REPO=your-org/target-repo-name
```

For true multi-repo routing (different issues going to different repos), configure the `REPO_MAPPING` environment variable with a JSON map of label → repo:

```env
REPO_MAPPING={"frontend": "your-org/frontend-repo", "backend": "your-org/backend-repo"}
```

> The pipeline uses component labels (e.g. `component:frontend`) on the issue to resolve which repository to commit code to.

#### Step 4 — Add the CI Workflow to Each Target Repo

Copy `.github/workflows/spec-driven-dev-ci.yml` to each target repository so that Stage 4 CI validation can run.

#### One Pipeline Run at a Time

The `notify-spec-driven-pipeline.yml` workflow uses a `concurrency` group (`spec-driven-pipeline-dispatch`) scoped per repository. This ensures that if multiple issues are opened rapidly in the same target repo, the dispatch calls are **serialised** — not cancelled. Combined with `MAX_CONCURRENT_TICKETS=1` on the server, only one pipeline run ever executes at a time.

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITHUB_TOKEN` | ✅ | — | GitHub PAT with `repo` + `workflow` scopes |
| `GITHUB_REPO` | ✅ | — | Target repo in `owner/repo` format |
| `GITHUB_WEBHOOK_SECRET` | ✅ | — | HMAC secret for webhook signature validation |
| `GITHUB_AUDIT_REPO` | ❌ | — | Repo to ship audit logs to (`owner/repo`) |
| `GITHUB_WORKFLOW_FILE` | ❌ | `<repo-name>-ci.yml` | Custom CI workflow filename |
| `NIM_API_KEY` | ✅ | — | NVIDIA NIM API key |
| `NIM_BASE_URL` | ✅ | `https://integrate.api.nvidia.com/v1` | NIM base URL |
| `NIM_MODEL` | ❌ | `z-ai/glm-5.3` | Primary LLM model |
| `NIM_FALLBACK_MODEL` | ❌ | `deepseek-ai/deepseek-v4-pro` | Fallback model after 3 consecutive failures |
| `QUALIFYING_LABEL` | ❌ | `SpecDrivenDev` | Label that marks an issue as eligible |
| `MAX_CODE_ITERATIONS` | ❌ | `6` | Max coding-loop iterations before escalation |
| `GATE_TIMEOUT_HOURS` | ❌ | `48` | Hours before a human gate times out |
| `BLOCKING_GATES` | ❌ | `true` | `false` = non-blocking (async) gate mode |
| `MAX_CONCURRENT_TICKETS` | ❌ | `2` | **Set to `1` for serial execution** |
| `RATE_LIMIT_CAP` | ❌ | `32` | Max NIM API calls per 60-second window |
| `CHECKPOINT_DIR` | ❌ | `./checkpoints` | Directory for per-ticket state files |
| `AUDIT_DIR` | ❌ | `./audit` | Directory for per-ticket audit JSONL logs |
| `SLACK_WEBHOOK_URL` | ❌ | — | Slack incoming webhook for notifications |
| `ESCALATION_EMAIL` | ❌ | — | Email for escalation alerts |
| `WEBHOOK_HOST` | ❌ | `0.0.0.0` | Webhook server bind host |
| `WEBHOOK_PORT` | ❌ | `8000` | Webhook server bind port |
| `REPO_MAPPING` | ❌ | `{}` | JSON map for multi-repo routing |

---

## Raising an Issue to Trigger the Pipeline

An issue **must** meet these criteria to be picked up:

### Required
- ✅ The issue must have the **`SpecDrivenDev`** label (configurable via `QUALIFYING_LABEL`).

### Recommended Issue Body Format

The pipeline reads the issue body for:
- **Acceptance Criteria** — under a section heading `## Acceptance Criteria`, `## Criteria`, or `## Requirements`, OR as a checklist (`- [ ] criterion`).
- **Description** — the overall body text.
- **Code hints** — function names, CSS class names, component identifiers mentioned in the title or body are extracted as symbols to guide file selection.

**Example issue:**

```markdown
## Summary
Add a loading spinner to the Login page while the authentication request is in-flight.

## Acceptance Criteria
- [ ] A spinner is shown when the Login button is clicked.
- [ ] The spinner disappears once the auth response arrives.
- [ ] The Login button is disabled while the request is in-flight.
- [ ] Existing login unit tests continue to pass.

## Technical Notes
- Component: `LoginForm.tsx`
- Spinner component: `LoadingSpinner`
```

---

## Pipeline Stages Explained

| Stage | Name | What Happens |
|---|---|---|
| **Stage 1** | Context Assembly | Fetches the issue, enumerates the repo file tree, ranks source and test files by relevance using BM25 + LLM + import-graph tracing, builds a `TicketContext`. |
| **Stage 2** | Test Generation + Gate 1 | LLM generates a test suite from acceptance criteria. Tests are committed to a `tests/` branch and a PR is opened. Pipeline waits for human approval. |
| **Stage 3** | Code Generation | LLM generates source code changes that make the tests pass. Runs up to `MAX_CODE_ITERATIONS` times with a CI-driven feedback loop. Locked test files cannot be modified. |
| **Stage 4** | CI Validation | Triggers the full CI workflow (ESLint, Semgrep, Trivy, tests, SonarQube). Parses gate results. On failure, attempts LLM-guided repair once before escalating. |
| **Stage 5** | PR Promotion + Gate 2 | Promotes the draft PR to ready-for-review, updates the PR description, notifies reviewers. Waits for final human approval. On "changes requested", routes feedback back to Stage 3. |

---

## Human Gates (Approval Points)

### Gate 1 — Test Review
- **When:** After Stage 2 generates tests.
- **What to do:** Review the test PR opened on the `tests/<ticket-id>` branch. Check that tests correctly capture the acceptance criteria. Either **Approve** the PR or **Request Changes**.
- **On approval:** Pipeline advances to Stage 3 (code generation).
- **On changes requested:** The LLM revises the tests up to `max_gate1_revision_cycles` (default: 5) times.

### Gate 2 — Final Code Review
- **When:** After Stage 4 CI validation passes.
- **What to do:** Review the final PR (now promoted from Draft to Ready for Review). Review code diffs, test results, and the CI gate table in the PR description.
- **On approval:** Pipeline merges, closes the issue, ships audit log.
- **On changes requested:** Leave review comments describing what needs to change. The pipeline reads the comments and routes back to Stage 3 for another iteration.

> **Timeouts:** If no review is given within `GATE_TIMEOUT_HOURS` (default: 48 hours), the pipeline escalates by posting a comment on the issue and halting.

---

## Webhook Server

### Starting

```bash
# Development
uvicorn webhook.app:app --host 0.0.0.0 --port 8000 --reload

# Production (with auto-restart)
uvicorn webhook.app:app --host 0.0.0.0 --port 8000 --workers 1
```

> Always use `--workers 1`. The queue and worker pool are in-process; multiple Uvicorn workers would have separate queues.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/github` | Receives GitHub Issue / PR review events |
| `GET` | `/health` | Returns worker status and queue length |
| `GET` | `/metrics` | Prometheus-format counters and gauges |

### Running Behind a Reverse Proxy (nginx)

```nginx
server {
    listen 443 ssl;
    server_name pipeline.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Hub-Signature-256 $http_x_hub_signature_256;
        proxy_set_header X-GitHub-Event $http_x_github_event;
    }
}
```

### Using ngrok for Local Development

```bash
ngrok http 8000
# Copy the Forwarding URL (e.g. https://abc123.ngrok.io)
# Set this as the Payload URL in GitHub webhook settings
```

---

## Monitoring & Health Checks

### Health Endpoint

```bash
curl https://your-server.example.com/health
```

Returns:
```json
{
  "status": "ok",
  "worker": "idle",
  "busy_workers": 0,
  "max_workers": 1,
  "active_tickets": [],
  "queue_length": 0,
  "tickets_processed": 12,
  "tickets_failed": 1
}
```

### Metrics Endpoint (Prometheus)

```bash
curl https://your-server.example.com/metrics
```

### Checkpoints

Each ticket's pipeline state is saved as a JSON file in `CHECKPOINT_DIR` (default: `./checkpoints/`). If the server restarts mid-pipeline, the ticket resumes automatically from its last completed stage.

```bash
ls ./checkpoints/
# 42.json  101.json  ...

cat ./checkpoints/42.json | python -m json.tool
```

### Audit Logs

Per-ticket JSONL audit logs are written to `AUDIT_DIR` (default: `./audit/`). Each line is a structured event (`pipeline_start`, `stage1_complete`, `gate1_approved`, etc.).

---

## Troubleshooting

### Pipeline Not Triggering

- Verify the webhook is active in GitHub: **Settings → Webhooks → Recent Deliveries**.
- Confirm the issue has the `SpecDrivenDev` label.
- Check the server logs: `uvicorn` output will show `Enqueued GitHub Issue #X`.
- Confirm `GITHUB_WEBHOOK_SECRET` matches what is configured in GitHub.

### Gate Not Advancing

- In **non-blocking mode** (`BLOCKING_GATES=false`), gates advance when GitHub sends a `pull_request_review` webhook. Make sure the webhook is configured to send that event type.
- Check the checkpoint file for `gate_waiting` field — if set, the pipeline is suspended waiting for review.
- You can manually re-trigger by sending a POST to `/webhooks/github` with a dummy `pull_request_review` payload, or by restarting the server (the stale-gate monitor will re-enqueue timed-out tickets on the next poll cycle).

### LLM Errors / Rate Limits

- The pipeline has a built-in `RATE_LIMIT_CAP` (default: 32 calls/60s) with an exponential-backoff retry.
- If the primary model fails 3 consecutive times, it falls back to `NIM_FALLBACK_MODEL`.
- Check audit logs in `./audit/<ticket-id>.jsonl` for `llm_error` events.

### Resuming After a Crash

The pipeline automatically resumes from the last checkpoint on the next run of the same `ticket_id`. No manual intervention is needed — just re-trigger the pipeline (re-label the issue or POST to the webhook directly).

### Tests Pass But Stage 4 Fails

- Check the CI run on GitHub Actions for the `automation/<ticket-id>` branch.
- Common causes: ESLint config missing, Semgrep rules, or test framework not installed in CI.
- The Stage 4 repair loop will attempt one LLM-guided fix before escalating.

---

## Project Structure

```
SpecDrivenDevAutomation/
├── orchestrator/
│   ├── main.py                  # Pipeline entry point & run_pipeline()
│   ├── config.py                # Pydantic settings (env vars)
│   ├── models.py                # TicketData, PipelineResult
│   ├── gate.py                  # Human gate polling (Gate 1 & 2)
│   ├── github_client.py         # GitHub VCS operations (PRs, commits, branches)
│   ├── github_issue_client.py   # GitHub Issues operations (fetch, label, comment)
│   ├── stages/
│   │   ├── stage1_context.py    # Context assembly & file ranking
│   │   ├── stage2_test_gen.py   # Test generation + Gate 1
│   │   ├── stage3_code_gen.py   # Iterative code generation
│   │   ├── stage4_validation.py # CI validation & gate parsing
│   │   └── stage5_pr.py         # PR promotion + Gate 2
│   └── concurrency.py           # Worker pool (serial execution)
├── webhook/
│   └── app.py                   # FastAPI webhook server
├── Target Repo Workflow/
│   └── .github/workflows/
│       └── notify-spec-driven-pipeline.yml   # Add this to every target repo
├── .github/workflows/
│   └── spec-driven-dev-ci.yml   # Add this to every target repo (CI validation)
├── .env.example                 # Environment variable template
├── requirements.txt
└── HowToUse.md                  # This file
```

