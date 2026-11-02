# Spec-Driven Development Pipeline — How to Use (GitLab & JIRA Version)

> **Automated AI-powered spec-to-code pipeline driven by JIRA Tickets and GitLab Repositories.**

---

## Table of Contents

1. [What This Pipeline Does](#what-this-pipeline-does)
2. [Architecture Overview](#architecture-overview)
3. [Prerequisites](#prerequisites)
4. [Quick Start (Single Repo)](#quick-start-single-repo)
5. [Multi-Repo Setup (GitLab Target Repo)](#multi-repo-setup-gitlab-target-repo)
6. [Environment Variables Reference](#environment-variables-reference)
7. [Raising a JIRA Ticket to Trigger the Pipeline](#raising-a-jira-ticket-to-trigger-the-pipeline)
8. [Pipeline Stages Explained](#pipeline-stages-explained)
9. [Human Gates (Approval Points)](#human-gates-approval-points)
10. [Webhook Server](#webhook-server)
11. [Monitoring & Health Checks](#monitoring--health-checks)
12. [Troubleshooting](#troubleshooting)

---

## What This Pipeline Does

The Spec-Driven Development Pipeline takes a **JIRA Ticket** (describing a feature or bug) and **automatically**:

1. Reads the ticket summary, description, and acceptance criteria from JIRA.
2. Selects relevant source files from the target GitLab repository using BM25 search, semantic ranking, and import-graph tracing.
3. Generates test cases that cover the acceptance criteria (Stage 2).
4. Opens a **Gate 1 Draft Merge Request (MR)** in GitLab for human review of the tests.
5. Once approved, generates source code changes in GitLab that make the tests pass (Stage 3).
6. Triggers and monitors the GitLab CI pipeline — ESLint, Semgrep SAST, Trivy SCA, Jest/Pytest, SonarQube (Stage 4).
7. Promotes the MR to ready-for-review and waits for a **Gate 2 human approval** (Stage 5).
8. On approval, the MR is merged, comments are posted back to JIRA, and the ticket status is transitioned.

Only **one pipeline run executes at a time** per worker. Additional triggers are queued and processed serially.

---

## Architecture Overview

```
 JIRA Ticket   ──►  Webhook Server  ──►  Orchestrator Pipeline
(SpecDrivenDev)    /webhooks/jira        Stage 1 → 2 → 3 → 4 → 5
                         │                         │
              ┌──────────┴──────────┐              ▼
              │   Stage 2 Gate 1   │   ← Human reviews test MR on GitLab
              │   Stage 5 Gate 2   │   ← Human approves code MR on GitLab
              └─────────────────────┘
```

- **Webhook Server** — FastAPI service that receives JIRA and GitLab webhook events, validates HMAC signatures / secret tokens, deduplicates, and enqueues tickets.
- **Orchestrator** — Python pipeline that runs all 5 stages serially. Checkpoints state to disk so it can resume after a crash or restart.
- **LLM Backend** — NVIDIA NIM API (configurable model, with fallback).
- **VCS & Issue Tracker** — JIRA REST API (via `atlassian-python-api`) and GitLab REST API (via `python-gitlab`).

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python 3.11+ | Tested on Python 3.13 |
| JIRA Cloud / Server | User email, API token, and project key |
| GitLab Instance | API token with `api`, `read_repository`, `write_repository` scopes |
| NVIDIA NIM API Key | From [build.nvidia.com](https://build.nvidia.com) |
| A GitLab repository | Target repository where code and tests will be committed |
| A server / VM with a public URL | To host the webhook server (or use `ngrok` for local dev) |

---

## Quick Start (Single Repo)

### 1. Clone and Install

```bash
git clone https://github.com/your-org/SpecDrivenDevAutomation.git
cd SpecDrivenDevAutomation
git checkout GitLab_JIRA_version
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```env
# JIRA
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_API_TOKEN=your-jira-api-token
JIRA_USER_EMAIL=your-email@example.com
JIRA_PROJECT_KEY=PROJ
JIRA_WEBHOOK_SECRET=your-jira-webhook-secret
QUALIFYING_LABEL=SpecDrivenDev

# GitLab
GITLAB_BASE_URL=https://gitlab.com
GITLAB_API_TOKEN=your-gitlab-private-token
GITLAB_REPO_ID=12345
GITLAB_WEBHOOK_SECRET=your-gitlab-webhook-secret

# LLM
NIM_API_KEY=your-nim-api-key
NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_MODEL=z-ai/glm-5.3

# Pipeline
MAX_CONCURRENT_TICKETS=1
```

### 3. Start the Webhook Server

```bash
uvicorn webhook.app:app --host 0.0.0.0 --port 8000
```

The server exposes:
- `POST /webhooks/jira` — receives JIRA ticket events
- `POST /webhooks/gitlab` — receives GitLab MR approval and comment events
- `GET /health` — liveness probe
- `GET /metrics` — Prometheus metrics

### 4. Configure Webhooks

#### In JIRA:
Go to **Jira Settings → System → Webhooks → Create a Webhook**:
- URL: `https://your-server.example.com/webhooks/jira`
- Events: Issue `created`, `updated`
- JQL filter: `labels = "SpecDrivenDev"`

#### In GitLab:
Go to your target project's **Settings → Webhooks → Add new webhook**:
- URL: `https://your-server.example.com/webhooks/gitlab`
- Secret Token: matches `GITLAB_WEBHOOK_SECRET`
- Trigger: `Merge request events`, `Comments`

### 5. Add CI Validation Workflow to Your GitLab Target Repo

Copy `Target Repo Workflow/.gitlab-ci.yml` into the root of your target GitLab repository. This provides the validation stages (tests, ESLint, Semgrep, Trivy, SonarQube) polled in Stage 4.

---

## Multi-Repo Setup (GitLab Target Repo)

For multi-repo setups where tickets map to different GitLab projects:
Configure `REPO_MAPPING` in `.env` as a JSON mapping of component/label to GitLab numeric Project ID:

```env
REPO_MAPPING={"frontend": 12345, "backend": 67890}
```

The orchestrator inspects the JIRA ticket's component or labels and routes commits and MRs to the matching GitLab project ID automatically.

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `JIRA_BASE_URL` | ✅ | — | JIRA instance root URL |
| `JIRA_API_TOKEN` | ✅ | — | JIRA API token |
| `JIRA_USER_EMAIL` | ✅ | — | JIRA user email |
| `JIRA_PROJECT_KEY` | ✅ | — | Target project key |
| `GITLAB_BASE_URL` | ✅ | `https://gitlab.com` | GitLab instance URL |
| `GITLAB_API_TOKEN` | ✅ | — | GitLab private API token |
| `GITLAB_REPO_ID` | ✅ | — | Target GitLab project numeric ID |
| `JIRA_WEBHOOK_SECRET` | ❌ | — | Shared secret for JIRA webhook HMAC-SHA256 |
| `GITLAB_WEBHOOK_SECRET` | ❌ | — | Token for GitLab webhook header verification |
| `NIM_API_KEY` | ✅ | — | NVIDIA NIM API key |
| `NIM_BASE_URL` | ✅ | `https://integrate.api.nvidia.com/v1` | NIM base URL |
| `NIM_MODEL` | ❌ | `z-ai/glm-5.3` | Primary LLM model |
| `NIM_FALLBACK_MODEL` | ❌ | `deepseek-ai/deepseek-v4-pro` | Fallback LLM model |
| `QUALIFYING_LABEL` | ❌ | `SpecDrivenDev` | Label marking tickets eligible |
| `MAX_CODE_ITERATIONS` | ❌ | `6` | Maximum code-repair cycles |
| `GATE_TIMEOUT_HOURS` | ❌ | `48` | Human review timeout |
| `BLOCKING_GATES` | ❌ | `true` | CLI blocking vs server non-blocking mode |
| `MAX_CONCURRENT_TICKETS` | ❌ | `2` | Number of concurrent pipeline workers |
| `RATE_LIMIT_CAP` | ❌ | `32` | Max NIM calls per 60s window |
| `CHECKPOINT_DIR` | ❌ | `./checkpoints` | Checkpoint state directory |
| `AUDIT_DIR` | ❌ | `./audit` | Audit logs directory |
| `REPO_MAPPING` | ❌ | `{}` | JSON map of component to GitLab project ID |

---

## Raising a JIRA Ticket to Trigger the Pipeline

1. Create a ticket in JIRA under your configured project key (e.g. `PROJ-101`).
2. Add the **`SpecDrivenDev`** label to the ticket.
3. Include an **Acceptance Criteria** section in the description or as a custom field:

```markdown
h2. Summary
Add loading state to payment submission button.

h2. Acceptance Criteria
* Disable the button immediately upon click.
* Display spinner icon while payment API is in-flight.
* Re-enable button if error occurs.
* Existing unit tests must pass.
```

---

## Pipeline Stages Explained

| Stage | Name | What Happens |
|---|---|---|
| **Stage 1** | Context Assembly | Pulls ticket from JIRA, enumerates GitLab repository tree, ranks source and test files using BM25 and import graph. |
| **Stage 2** | Test Generation + Gate 1 | Generates test suite from acceptance criteria, creates branch on GitLab, opens Draft MR, awaits human approval. |
| **Stage 3** | Code Generation | Implements source changes to pass tests. Runs iterative loop with CI feedback. Locked test files are protected against tampering. |
| **Stage 4** | CI Validation | Triggers GitLab CI pipeline (`.gitlab-ci.yml`), validates all gates (Lint, SAST, SCA, Tests, SonarQube). |
| **Stage 5** | MR Promotion + Gate 2 | Promotes MR to ready-for-review, updates description, notifies reviewers, awaits final human approval. |

---

## Human Gates (Approval Points)

### Gate 1 — Test Review
- **Where:** GitLab Merge Request on `tests/<ticket-id>` branch.
- **Action:** Review test assertions. Approve or leave revision comments (`/approve` or review comments).
- **On approval:** Pipeline advances to Stage 3.

### Gate 2 — Final Review
- **Where:** GitLab Merge Request on `automation/<ticket-id>` branch.
- **Action:** Review implementation diffs and CI validation status. Approve or request changes.
- **On approval:** MR is merged, JIRA ticket is closed, and audit log is shipped.
