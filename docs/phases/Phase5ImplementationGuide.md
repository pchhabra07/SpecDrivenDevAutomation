# Phase 5 Implementation Guide: Concurrency, Multi-Repo Routing, Transcript Pruning, and Metrics Evaluation

## 1. Executive Summary

Phase 5 completes the Spec-Driven Development Automation Pipeline by introducing production-grade scalability, multi-worker concurrency, multi-repository routing, context window pruning, and PRD Section 8 success metrics evaluation.

Key highlights:
- **Zero Rate-Limit Breaches**: Thread-safe token bucket rate limiter shared across concurrent workers guarantees strict adherence to the NVIDIA NIM free-tier ceiling ($\le 32$ calls/min).
- **Asynchronous Multi-Worker Pool**: `ConcurrentWorkerPool` enables processing multiple tickets concurrently with automatic worker failure isolation and queue drain management.
- **Dynamic Repository Resolution**: `RepoResolver` inspects incoming JIRA ticket components, labels, and project keys to dynamically route code and test generation to arbitrary GitLab repositories.
- **Context Window Pruner**: Slidng-window pruner prevents transcript bloating on multi-iteration tickets, keeping prompt token usage within strict model context constraints.
- **Automated KPI Reporting**: `metrics_engine.py` reads JSONL audit logs to compute PRD Section 8 metrics (First-Time Pass Rate, PR Acceptance Rate, Human Interventions, Cycle Time) with Markdown and JSON outputs.

---

## 2. Concurrency Architecture

### Multi-Worker Processing Model

```
                    ┌─────────────────────────┐
                    │ JIRA Webhook Notification│
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │ FastAPI Webhook Server  │
                    └───────────┬─────────────┘
                                │ enqueue(ticket_id)
                                ▼
                    ┌─────────────────────────┐
                    │      Ticket Queue       │
                    └───────────┬─────────────┘
                                │
          ┌─────────────────────┴─────────────────────┐
          ▼                                           ▼
┌──────────────────┐                        ┌──────────────────┐
│  Worker Task 1   │                        │  Worker Task 2   │
│ (Ticket PROJ-101)│                        │ (Ticket CORE-202)│
└─────────┬────────┘                        └─────────┬────────┘
          │                                           │
          │ acquire()                                 │ acquire()
          ▼                                           ▼
┌──────────────────────────────────────────────────────────────┐
│          Shared RateLimiter (max_calls <= 32/min)            │
│         Thread-safe with thread lock & sliding window        │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │       NVIDIA NIM API         │
                └──────────────────────────────┘
```

### Worker Isolation & Error Boundary
In `orchestrator/concurrency.py`:
- `ConcurrentWorkerPool` spawns worker loops bounded by an `asyncio.Semaphore(max_concurrent_tickets)`.
- If a worker encounters an unhandled exception or pipeline failure on ticket `A`, the error is trapped, logged, and audited. The worker returns to the pool to process ticket `B` without crashing the daemon.
- System metrics are exposed live via `/metrics` and `/health` endpoints in FastAPI.

---

## 3. Dynamic Multi-Repository Routing

The pipeline is no longer pinned to a single hardcoded repository. `RepoResolver` inspects ticket metadata and matches it against `REPO_MAPPING`.

### Configuration Format (`.env`)

```env
# JSON map of Component name, Label, or Project prefix to GitLab Project ID
REPO_MAPPING={"Payments": 101, "auth-service": 201, "billing": 301}
```

### Resolution Rules

1. **JIRA Components**:
   - If ticket contains `components: ["Payments"]`, resolves to `101`.
   - Matching is case-insensitive.
2. **JIRA Labels**:
   - Direct label match: `labels: ["auth-service"]` $\rightarrow$ `201`.
   - Prefix match: `labels: ["repo:301"]` $\rightarrow$ `301` directly; `labels: ["repo:billing"]` $\rightarrow$ `301`.
3. **Ticket Project Key**:
   - Ticket key prefix `BILLING-42` matches key `"billing"` in mapping $\rightarrow$ `301`.
4. **Fallback Default**:
   - If no rule matches or mapping is empty, defaults to `GITLAB_REPO_ID`.

---

## 4. Context Window Pruning

### Problem Addressed
During multi-iteration tickets (Stage 3 code-generation retry loops or Stage 5 human reviewer revisions), sending full accumulated prompt history repeatedly can exceed the model's context window.

### Pruning Heuristic (`orchestrator/context_pruner.py`)
- **Preserved Head**:
  - Turn 0: System prompt (`"You are a spec-driven development agent..."`).
  - Turn 1: Initial ticket context and acceptance criteria.
- **Condensed Middle Turns**:
  - Turns between Turn 2 and Turn $N - \text{keep\_last\_turns}$ have their verbose contents (large code blocks, test output logs) truncated with informative markers (`"[Condensed assistant turn (1200 chars)]: ... [truncated] ..."`).
- **Preserved Tail**:
  - The trailing $K$ turns (default 4) containing the active diff, latest Jest trace, and reviewer feedback remain untouched.
- **Activation**:
  - Triggered automatically inside `LLMClient._call_with_retry` whenever `ENABLE_CONTEXT_PRUNING=true` and token count exceeds `MAX_CONTEXT_TOKENS` (default 16,000).

---

## 5. Success Metrics Evaluation Engine

`orchestrator/metrics_engine.py` implements automated evaluation for all metrics specified in **PRD Section 8**:

| Metric | Target | Formula |
|---|---|---|
| **First-Time Test Pass Rate** | $\ge 60\%$ | Tickets passing Stage 4 with iteration count $= 1$ / completed tickets |
| **PR Acceptance Rate** | $\ge 85\%$ | Promoted PRs merged without manual rework / total promoted PRs |
| **Tamper Prevention Rate** | $100\%$ | 0 unauthorized modifications to locked test suite |
| **Average Iterations to Green** | $\le 2.5$ | Mean Stage 3 iteration count for successful tickets |
| **End-to-End Cycle Time** | $\le 30$ min | Mean elapsed time from qualification to Gate 2 promotion |
| **Human Intervention Rate** | $\le 20\%$ | Tickets requiring human escalation or rejected triage |

### CLI Usage

Generate Markdown report in console:
```bash
python -m orchestrator.metrics_engine
```

Generate report and save to file:
```bash
python -m orchestrator.metrics_engine --output audit_logs/metrics_report.md
```

Export raw metrics in JSON format:
```bash
python -m orchestrator.metrics_engine --json
```

Specify custom audit directory:
```bash
python -m orchestrator.metrics_engine --audit-dir path/to/logs --output report.md
```

---

## 6. Configuration Reference

| Variable | Type | Default | Description |
|---|---|---|---|
| `MAX_CONCURRENT_TICKETS` | int | `3` | Maximum number of tickets processed concurrently by the webhook pool. |
| `REPO_MAPPING` | string (JSON) | `{}` | JSON dictionary mapping component/label/project keys to GitLab repo IDs. |
| `ENABLE_CONTEXT_PRUNING` | bool | `true` | Enables sliding-window context pruning in `LLMClient`. |
| `MAX_CONTEXT_TOKENS` | int | `16000` | Token threshold above which transcript pruning is activated. |
| `RATE_LIMIT_CAP` | int | `32` | Maximum LLM API calls allowed per 60-second rolling window. |

---

## 7. Operational & Verification Procedures

### Running the Full Test Suite
To verify all unit tests and components across Phases 1–5:
```bash
python -m pytest tests/unit/ -v
```

Expected result: 122+ passing unit tests with 0 failures.

### Starting the Concurrent Webhook Server
```bash
python -m orchestrator.main --server
```
or via Uvicorn:
```bash
python -m uvicorn webhook.app:app --host 0.0.0.0 --port 8000
```

### Checking Live Worker Telemetry
Query the `/metrics` endpoint:
```bash
curl http://localhost:8000/metrics
```

Sample response:
```json
{
  "concurrency": {
    "queue_depth": 0,
    "active_workers": 1,
    "processed_count": 5,
    "failed_count": 0,
    "active_tickets": ["PROJ-102"]
  },
  "rate_limiter": {
    "calls_in_last_minute": 14,
    "cap": 32
  }
}
```
