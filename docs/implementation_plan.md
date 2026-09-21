# Implementation Plan
# Automated Ticket-to-PR Pipeline: JIRA → AI Coding Agent → GitLab PR

## Overview

This plan describes how to build the spec-driven automation pipeline defined in the PRD. All open questions from the initial draft are now resolved and incorporated below. No further clarification is needed before execution.

**Stack summary:**
- Orchestrator: Python 3.12 (single-process, checkpointed, rate-limited)
- Target repositories: JavaScript / TypeScript / React / Redux
- Test runner: Jest (Istanbul-based coverage)
- LLM: GLM-5.3 (Z.ai) via NVIDIA NIM, OpenAI-compatible API
- Quality gates: ESLint → Semgrep → Trivy → Jest coverage → SonarQube (single consolidated gate)
- Human approval mechanism: GitLab MR approval (same implementation for Gate 1 and Gate 2)

---

## Architecture

```
JIRA Webhook (Phase 3+)  /  CLI (Phase 1-2)
            │
            ▼
┌───────────────────────────────────────────────────────────────────────┐
│                        Orchestrator (Python)                          │
│  Serial, single-ticket processing. Rate-limited to ≤32 NIM calls/min │
│  Full state checkpointed to disk after every iteration.               │
│                                                                       │
│  Stage 1 ─ Assemble Ticket Context                                    │
│            (JIRA REST + GitLab file tree/test suite)                  │
│                                                                       │
│  Stage 2 ─ Generate Test Cases                                        │
│            Push to branch: automation/<ticket-id>/tests               │
│            Open "tests-only" GitLab MR ──► [Human Gate 1]            │
│            Poll GitLab API for MR approval                            │
│            On approval: lock test files (store SHA-256 hashes)        │
│                                                                       │
│  Stage 3 ─ Iterative Code Generation (max 6 iterations)              │
│            LLM generates JS/TS diffs → apply → run Jest               │
│            After each iteration: tamper-check locked test hashes      │
│            On tamper: halt + flag for human                           │
│            On N=6 exhausted: escalation path (see below)             │
│                                                                       │
│  Stage 4 ─ CI/CD Validation (GitLab CI pipeline)                     │
│            ESLint → Semgrep → Trivy → Jest --coverage                 │
│            SonarQube ingests results → single pass/fail gate          │
│                                                                       │
│  Stage 5 ─ Pull Request                                               │
│            Promote draft MR to ready-for-review                       │
│            MR description: diffs + test output + gate results + JIRA  │
│            ──► [Human Gate 2: GitLab MR approval]                     │
└───────────────────────────────────────────────────────────────────────┘
        │
        ├── GitLab API  (branches, commits, MR create/poll/promote)
        ├── JIRA REST API  (ticket read, comment, status transition)
        ├── NVIDIA NIM API  (GLM-5.3, OpenAI-compatible)
        └── GitLab CI/CD  (Jest, ESLint, Semgrep, Trivy, SonarQube)
```

### Human Gate — Shared Implementation

Both Gate 1 (tests review) and Gate 2 (final PR review) use the **same approval-detection mechanism**:

1. Orchestrator creates (or promotes) a GitLab MR.
2. Orchestrator polls the GitLab MR Approvals API on a configurable interval until the required number of approvals is reached.
3. Optionally, a GitLab webhook can push an approval event to the webhook service (Phase 3+) to avoid polling.

This means one `gitlab_gate.py` module handles both gates.

### Escalation Path (N = 6 Iterations)

When the coding loop exhausts 6 iterations without all target tests passing:

1. Push the branch as-is to GitLab (nothing discarded).
2. Keep/promote the existing draft MR; apply the label `partial-needs-human`.
3. Populate the MR description with an auto-generated summary: iterations attempted, final Jest pass/fail breakdown, agent's last assessment of the blocker.
4. Post the same summary as a comment on the originating JIRA ticket.
5. Send a Slack/email notification to the assigned reviewer (configurable channels via env vars).
6. Halt the pipeline; do **not** proceed to Stage 4 or Stage 5.

---

## Proposed Changes

### Repository Structure

```
SpecDrivenDevAutomation/
├── orchestrator/
│   ├── main.py
│   ├── config.py
│   ├── rate_limiter.py
│   ├── checkpoint.py
│   ├── llm_client.py
│   ├── audit.py
│   ├── tamper_check.py
│   ├── gitlab_client.py
│   ├── gitlab_gate.py          ← shared Gate 1 & Gate 2 approval detection
│   ├── jira_client.py
│   ├── notifier.py             ← Slack/email for escalation
│   ├── ci_poller.py
│   └── stages/
│       ├── stage1_context.py
│       ├── stage2_test_gen.py
│       ├── stage3_code_gen.py
│       ├── stage4_validation.py
│       └── stage5_pr.py
├── webhook/
│   ├── app.py
│   └── requirements.txt
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── ci_templates/
│   └── gitlab-ci-template.yml  ← CI pipeline template for target repos
├── tests/
│   ├── unit/
│   └── integration/
├── requirements.txt
├── .env.example
└── README.md
```

---

### Component: Orchestrator Core

---

#### [NEW] `orchestrator/config.py`

Centralised settings loaded exclusively from environment variables. No defaults for secrets.

| Variable | Description | Default |
|---|---|---|
| `JIRA_BASE_URL` | JIRA instance root URL | — |
| `JIRA_API_TOKEN` | JIRA API token | — |
| `JIRA_PROJECT_KEY` | Project key filter | — |
| `QUALIFYING_LABEL` | Label that triggers automation | `SpecDrivenDev` |
| `GITLAB_BASE_URL` | GitLab instance URL | — |
| `GITLAB_API_TOKEN` | GitLab personal/project token | — |
| `GITLAB_REPO_ID` | Target repo numeric ID | — |
| `NIM_API_KEY` | NVIDIA NIM API key | — |
| `NIM_BASE_URL` | NIM OpenAI-compatible base URL | — |
| `NIM_MODEL` | Primary model slug | `z-ai/glm-5.3` |
| `NIM_FALLBACK_MODEL` | Fallback model slug | `deepseek-ai/deepseek-v4-pro` |
| `RATE_LIMIT_CAP` | Max NIM calls per 60s | `32` |
| `CHECKPOINT_DIR` | Directory for per-ticket checkpoints | `./checkpoints` |
| `AUDIT_DIR` | Directory for audit JSONL logs | `./audit` |
| `MAX_CODE_ITERATIONS` | Max code-gen iterations before escalation | `6` |
| `COVERAGE_THRESHOLD` | Minimum Jest coverage % | `80` |
| `SONAR_HOST_URL` | SonarQube server URL | — |
| `SONAR_TOKEN` | SonarQube auth token | — |
| `GATE_POLL_INTERVAL_SECONDS` | How often to poll GitLab MR for approval | `30` |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook for escalation alerts | — |
| `ESCALATION_EMAIL` | Email for escalation notifications | — |

---

#### [NEW] `orchestrator/rate_limiter.py`

Sliding-window in-process rate limiter. Maintains a `deque` of UTC timestamps for outgoing NIM calls. Before every LLM call:
1. Evict timestamps older than 60 seconds.
2. If `len(deque) >= RATE_LIMIT_CAP`, sleep until the oldest timestamp exits the window.
3. Record the new call timestamp and proceed.

Implements FR-27, FR-28.

---

#### [NEW] `orchestrator/checkpoint.py`

Per-ticket checkpoint stored as `{CHECKPOINT_DIR}/{ticket_id}.json`. Written after every state transition (stage change) and after every code-gen iteration.

Checkpoint schema:
```json
{
  "ticket_id": "PROJ-123",
  "current_stage": 3,
  "iteration_count": 2,
  "locked_test_hashes": { "src/__tests__/foo.test.ts": "sha256hex..." },
  "conversation_history": [...],
  "modified_files": ["src/foo.ts"],
  "last_test_results": { "passed": 4, "failed": 1, "details": "..." },
  "gate1_mr_iid": 42,
  "draft_mr_iid": 43
}
```

On startup, if a checkpoint exists for the given ticket ID, the orchestrator resumes from `current_stage` / `iteration_count` without re-running earlier stages. Implements FR-30.

---

#### [NEW] `orchestrator/llm_client.py`

Wraps the `openai` Python SDK configured to the NIM base URL. Responsibilities:
- Maintains the **full in-memory conversation history** (system prompt + all user/assistant/tool messages) for the current ticket. Never evicted during rate-limit pauses (FR-29).
- Routes all calls through `rate_limiter.py`.
- Supports tool-call round-trips (send → receive tool-call → execute tool → send tool result → repeat).
- On any 5xx or rate-limit (429) response from NIM: exponential backoff up to 3 retries, then raise.
- Automatically falls back to `NIM_FALLBACK_MODEL` after 3 consecutive primary-model failures.

---

#### [NEW] `orchestrator/tamper_check.py`

```python
def check_tampering(locked_hashes: dict[str, str], repo_path: str) -> list[str]:
    """Returns list of file paths whose SHA-256 no longer matches the locked hash."""
```

Called after every code-gen iteration. If any paths are returned, `stage3_code_gen.py` halts the loop and triggers the escalation path.

---

#### [NEW] `orchestrator/audit.py`

Appends one JSON line per event to `{AUDIT_DIR}/{ticket_id}.jsonl`. Every entry includes `timestamp`, `ticket_id`, `stage`, `event_type`, and a `payload` dict. Event types:

- `pipeline_start`, `pipeline_complete`, `pipeline_error`
- `gate1_opened`, `gate1_approved`, `gate1_rejected`
- `iteration_start`, `iteration_result`, `tamper_detected`
- `escalation_triggered`
- `ci_gate_result` (one per CI stage)
- `pr_created`, `gate2_approved`, `gate2_merged`

Implements FR-25.

---

#### [NEW] `orchestrator/gitlab_client.py`

Wraps `python-gitlab`. Key methods:

| Method | Used in stage |
|---|---|
| `get_file_tree(ref)` | Stage 1 |
| `get_file_content(path, ref)` | Stage 1 |
| `create_branch(name, from_ref)` | Stage 2 |
| `commit_files(branch, files, message)` | Stage 2, Stage 3 |
| `create_mr(source_branch, target_branch, title, description, draft)` | Stage 2, Stage 5 |
| `get_mr_approvals(mr_iid)` | Gate polling |
| `promote_mr_to_ready(mr_iid)` | Stage 5 |
| `add_mr_label(mr_iid, label)` | Escalation |
| `trigger_pipeline(branch, variables)` | Stage 4 |
| `get_pipeline_status(pipeline_id)` | Stage 4 |

---

#### [NEW] `orchestrator/gitlab_gate.py`

Shared approval-detection logic used by both Gate 1 and Gate 2:

```python
def wait_for_approval(mr_iid: int, timeout_hours: float = 48) -> GateResult:
    """
    Polls the GitLab MR Approvals API every GATE_POLL_INTERVAL_SECONDS.
    Returns GateResult(approved=True/False, approver, timestamp) or
    raises GateTimeout if timeout_hours is exceeded.
    """
```

Both `stage2_test_gen.py` and `stage5_pr.py` call this function. No duplicate approval-detection code.

---

#### [NEW] `orchestrator/jira_client.py`

Wraps JIRA REST API (v3). Key methods:
- `get_ticket(ticket_id)` → returns summary, description, acceptance criteria, labels, assignee.
- `post_comment(ticket_id, body)` → used for escalation summary.
- `transition_ticket(ticket_id, transition_name)` → e.g., mark as "In Progress", "Done", or "Escalated".

Ticket is only processed if it carries the label matching `QUALIFYING_LABEL` (checked in webhook filter and re-verified here as a safety net).

---

#### [NEW] `orchestrator/notifier.py`

Handles escalation notifications:
- `notify_slack(message)` — HTTP POST to `SLACK_WEBHOOK_URL`.
- `notify_email(subject, body)` — SMTP send to `ESCALATION_EMAIL` (via Python `smtplib`; SMTP config from env vars).

Used exclusively by the escalation path in `stage3_code_gen.py`.

---

#### [NEW] `orchestrator/ci_poller.py`

Triggers a GitLab CI/CD pipeline for a given branch + commit SHA via `gitlab_client.py`, then polls `get_pipeline_status` until the pipeline reaches a terminal state (`success`, `failed`, `canceled`). Returns a structured `CIResult` with per-stage status and artifact URLs.

---

### Component: Pipeline Stages

---

#### [NEW] `orchestrator/stages/stage1_context.py`

**Inputs:** ticket ID  
**Outputs:** `TicketContext` dataclass

1. Call `jira_client.get_ticket()` — extract summary, description, acceptance criteria, labels.
2. Resolve target repo: look up `GITLAB_REPO_ID` from config (Phase 1–2: single repo; Phase 4+: mapping table keyed by JIRA label).
3. Call `gitlab_client.get_file_tree()` to enumerate source and test files.
4. Fetch existing test files (paths matching `**/*.test.ts`, `**/*.spec.ts`, `**/*.test.js`, `**/*.spec.js`).
5. Fetch the subset of source files most relevant to the ticket (LLM-assisted relevance ranking against the ticket description).
6. Write `TicketContext` to checkpoint.

---

#### [NEW] `orchestrator/stages/stage2_test_gen.py`

**Inputs:** `TicketContext`  
**Outputs:** locked test file hashes in checkpoint, `gate1_mr_iid` in checkpoint

1. Prompt GLM-5.3 with ticket spec + acceptance criteria + existing test suite to generate new/modified Jest test cases (`.test.ts` / `.spec.ts`).
2. Create branch `automation/<ticket-id>/tests` from the repo default branch.
3. Commit only test files to this branch. No source code changes.
4. Open a **GitLab MR** from `automation/<ticket-id>/tests` → default branch, titled `[Gate 1] Tests for <ticket-id>: <summary>`, marked as **draft**.
5. Call `gitlab_gate.wait_for_approval(gate1_mr_iid)`.
   - If approved: proceed.
   - If reviewer requests changes: re-prompt LLM with reviewer comments, regenerate, force-push to the same branch, re-await approval. (Loop until approved or manually abandoned.)
6. On approval:
   - Merge the tests-only MR (squash commit).
   - Compute SHA-256 of every locked test file and store in checkpoint.
   - From this point forward, `stage3_code_gen.py` has no write access to test file paths (enforced by commit filter in `gitlab_client.commit_files`).

Implements FR-5 through FR-8.

---

#### [NEW] `orchestrator/stages/stage3_code_gen.py`

**Inputs:** `TicketContext` + locked test hashes from checkpoint  
**Outputs:** updated checkpoint with modified source files + final test results, or escalation

Loop (up to `MAX_CODE_ITERATIONS = 6`):

1. Prompt GLM-5.3 with: ticket spec, current source files, locked test contents, previous iteration's Jest output, previous diffs.
2. Parse LLM response for file diffs / new file contents. **Reject any diff that touches a locked test path** — log the attempt and prompt the LLM to try again without modifying test files (counts as a sub-attempt within the same iteration, not a new iteration).
3. Apply diffs to the working branch `automation/<ticket-id>`.
4. Commit to the branch (draft MR was opened at iteration 1 — reused for subsequent iterations).
5. Trigger Jest run via GitLab CI `test` stage only. Wait for result via `ci_poller.py`.
6. **Tamper-check:** call `tamper_check.check_tampering()`. If non-empty list returned: halt, log to audit, post JIRA comment, notify via `notifier.py`, stop loop.
7. Record iteration result in checkpoint and audit log.
8. If all target tests pass: exit loop → proceed to Stage 4.
9. If iteration count reaches 6 and tests still failing: **escalation path** (see above). Stop.

Implements FR-9 through FR-11.

---

#### [NEW] `orchestrator/stages/stage4_validation.py`

**Inputs:** branch name + last successful commit SHA  
**Outputs:** `ValidationResult` dataclass with per-gate status

Triggers the full GitLab CI pipeline (all stages) and waits for completion:

| CI Stage | Tool | Pass Criteria |
|---|---|---|
| `lint` | ESLint (project config) | 0 errors |
| `sast` | Semgrep (default ruleset + custom) | 0 high/critical findings |
| `dependency-scan` | Trivy (filesystem scan) | 0 critical CVEs |
| `test-coverage` | Jest `--coverage` | Line coverage ≥ `COVERAGE_THRESHOLD` |
| `quality-gate` | SonarQube Community Edition | Quality Gate: Passed |

SonarQube ingests ESLint, Semgrep, and Jest LCOV reports via `sonar-scanner` in the CI step — it is the **single pass/fail gate** the orchestrator checks. All four tool outputs feed into it; Stage 5 proceeds only if `quality-gate` passes.

All gate results logged to audit (one `ci_gate_result` entry per stage). Implements FR-12 through FR-16.

---

#### [NEW] `orchestrator/stages/stage5_pr.py`

**Inputs:** `ValidationResult` + draft MR IID from checkpoint  
**Outputs:** MR URL, `gate2_mr_iid` in checkpoint

1. Promote the draft MR to ready-for-review via `gitlab_client.promote_mr_to_ready()`.
2. Update MR description to include:
   - Summary of code changes (auto-generated from diffs)
   - Full Jest test output (last iteration)
   - CI gate results (ESLint, Semgrep, Trivy, SonarQube)
   - Link to originating JIRA ticket (`Closes PROJ-123`)
   - Iteration count used
3. Call `gitlab_gate.wait_for_approval(gate2_mr_iid)` — polls until a human approves the MR.
4. On approval: log `gate2_approved` to audit. (Merge is performed by the reviewer in GitLab — no auto-merge.)
5. If reviewer requests changes: route back to Stage 3 with reviewer comments appended to the LLM conversation history (FR-21).

Implements FR-17 through FR-21.

---

### Component: Webhook Service (Phase 3)

---

#### [NEW] `webhook/app.py`

Lightweight **FastAPI** server. Receives JIRA webhook POSTs:

1. Validates JIRA webhook secret (HMAC-SHA256, `JIRA_WEBHOOK_SECRET` env var).
2. Extracts `issue.id`, `issue.fields.labels`, `issue.fields.status`, `webhookEvent`.
3. Filters: only process if `QUALIFYING_LABEL` (`SpecDrivenDev`) is present in labels AND the transition is to a configured qualifying status.
4. Enqueues the ticket ID into an in-process `asyncio.Queue`.
5. The orchestrator's main loop dequeues one ticket at a time (serial processing).

Implements FR-1, FR-2.

---

### Component: CI Pipeline Template

---

#### [NEW] `ci_templates/gitlab-ci-template.yml`

Template `.gitlab-ci.yml` to be included (or referenced via `include:`) in each target repo. Defines:

```yaml
stages:
  - test
  - lint
  - sast
  - dependency-scan
  - test-coverage
  - quality-gate

test:
  stage: test
  script: npx jest --testPathPattern=$TEST_PATTERN --json --outputFile=jest-results.json

lint:
  stage: lint
  script: npx eslint . --format json --output-file eslint-report.json

sast:
  stage: sast
  script: semgrep --config=auto --json --output semgrep-report.json .

dependency-scan:
  stage: dependency-scan
  image: aquasec/trivy:latest
  script: trivy fs --format json --output trivy-report.json .

test-coverage:
  stage: test-coverage
  script: npx jest --coverage --coverageReporters=lcov

quality-gate:
  stage: quality-gate
  script:
    - sonar-scanner -Dsonar.host.url=$SONAR_HOST_URL
                    -Dsonar.login=$SONAR_TOKEN
                    -Dsonar.sources=src
                    -Dsonar.javascript.lcov.reportPaths=coverage/lcov.info
                    -Dsonar.eslint.reportPaths=eslint-report.json
    - ./scripts/check_sonar_gate.sh  # polls SonarQube API for gate status
  artifacts:
    reports:
      junit: jest-results.json
```

---

### Component: Infrastructure

---

#### [NEW] `docker/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orchestrator/ ./orchestrator/
COPY webhook/ ./webhook/
ENV PYTHONPATH=/app
CMD ["python", "-m", "orchestrator.main"]
```

No credentials in image. All secrets injected at runtime via `--env-file`. Implements FR-22, FR-23.

#### [NEW] `docker/docker-compose.yml`

Two services: `orchestrator` and `webhook`. Shared named volume for `checkpoints/` and `audit/`. Env-file mount for `.env`.

---

#### [NEW] `requirements.txt`

```
openai>=1.40
python-gitlab>=4.5
atlassian-python-api>=3.41
pydantic>=2.7
fastapi>=0.111
uvicorn[standard]>=0.30
httpx>=0.27
```

#### [NEW] `.env.example`

Template for all env vars listed in `config.py`. No secret values. Committed to repo.

---

## Phase Delivery Plan

| Phase | Scope | Deliverables |
|---|---|---|
| **Phase 1** | PoC: CLI trigger, Stage 1 + Stage 2 + Gate 1 (MR approval) only | Orchestrator core + Stages 1–2 + `gitlab_gate.py` + checkpoint + rate limiter |
| **Phase 2** | Add Stage 3 (iterative code gen + tamper check) + Stage 4 (full CI gates) | Stage 3 + Stage 4 + `ci_templates/gitlab-ci-template.yml` + SonarQube integration |
| **Phase 3** | Full automation: JIRA webhook + Stage 5 (PR promotion + Gate 2) + escalation + notifications | Webhook service + Stage 5 + `notifier.py` + JIRA comment posting |
| **Phase 4** | Pilot rollout | Deploy to infra, restrict to qualifying repos, enable audit log shipping + alerting |
| **Phase 5** | Evaluate + scale | Assess serial bottleneck, tune N=6, consider concurrent processing or paid NIM tier |

---

## Security Implementation Notes (FR-22 to FR-25)

- Orchestrator runs in an ephemeral Docker container; secrets are never baked into the image or written to disk in plaintext.
- `GITLAB_API_TOKEN` is scoped: read-only during Stage 1; developer-write to `automation/*` branches only during Stages 2–3; no direct write to the default (protected) branch.
- `llm_client.py` never inserts raw secret values into the LLM conversation context. File contents passed to the LLM are stripped of any `.env`-like patterns before being included in prompts.
- `tamper_check.py` runs **after every iteration** — a compromised model that tries to weaken test assertions is caught immediately and halts the loop.
- Audit JSONL files are append-only; file mode `0o644`; committed to a separate audit repo (or shipped to a SIEM) in Phase 4.

---

## Verification Plan

### Automated Tests (Orchestrator — Python)

| Phase | Command | What It Validates |
|---|---|---|
| Phase 1 | `pytest tests/unit/ -v` | rate_limiter, checkpoint, tamper_check, llm_client (mocked NIM), gitlab_gate, stage1 + stage2 with fixture data |
| Phase 2 | `pytest tests/integration/ -v` | End-to-end run against a sandbox GitLab project with a synthetic JS/TS ticket |
| Phase 3 | `pytest tests/unit/test_webhook.py -v` | Webhook HMAC validation, label filtering, queue enqueue |

### Manual Verification Checklist

1. **Gate 1 flow** — Run Phase 1 CLI with a sample JIRA ticket fixture. Confirm: test files committed to `automation/<ticket-id>/tests` branch, MR opened as draft, approval detected, hashes locked in checkpoint.
2. **Tamper-check halt** — Manually edit a locked test file on the working branch during Stage 3. Confirm: loop halts, tamper is logged to audit, escalation notification sent, pipeline does not proceed to Stage 4.
3. **Rate-limit enforcement** — Set `RATE_LIMIT_CAP=5`; run a ticket that requires >5 LLM calls. Confirm: calls pause at cap, resume automatically after the 60-second window, no context lost across the pause.
4. **Checkpoint recovery** — Kill the orchestrator mid-Stage 3 (iteration 3 of 6). Restart with the same ticket ID. Confirm: resumes at iteration 4, previous iterations not repeated, conversation history intact.
5. **Escalation path** — Run a ticket with a deliberately unsolvable bug. Confirm at iteration 6: draft MR labeled `partial-needs-human`, JIRA comment posted, Slack/email notification sent, pipeline halted cleanly.
6. **Full end-to-end (Phase 3)** — Fire a real JIRA webhook for a ticket labeled `SpecDrivenDev`. Observe all 5 stages complete. Verify the resulting GitLab MR: correct description, Jest output, SonarQube gate result, JIRA link, and no auto-merge.
