# Pilot Rollout & Operational Runbook (Phase 4)

This document establishes the operational runbook and 4-week pilot deployment schedule for the **Spec-Driven Development Automation Pipeline**.

---

## 1. 4-Week Phased Rollout Schedule

```
Week 1: Staging Sandbox ──► Week 2: Shadow Mode ──► Week 3: Active Pilot ──► Week 4: Metrics Evaluation
(Synthetic tickets)         (Historical tickets)    (10–15 Live tickets)    (PRD Target Review)
```

### Week 1: Synthetic Sandbox Pilot
- **Objective**: Validate container deployment, webhook ingestion, rate-limit pauses, and all stage transitions in an isolated sandbox repository.
- **Scope**: 5 synthetic tickets covering:
  - Simple bug fix (happy path)
  - Edge-case bug requiring 2–3 iterations
  - Deliberately unsolvable bug (verifying escalation at iteration 6)
  - Tamper injection test (verifying halt on unauthorized test modification)
  - Ineligible ticket (verifying triage rejection)
- **Exit Criteria**: All 5 test scenarios execute cleanly without manual pipeline intervention.

### Week 2: Shadow Mode on Pilot Repo
- **Objective**: Onboard the first low-risk production repository (e.g. internal React UI library) and run automation on historical/closed tickets.
- **Scope**: 5 historical bugs re-opened in shadow mode.
- **Review**: Engineering team performs 100% manual inspection of generated tests and diffs.
- **Exit Criteria**: Generated test suites achieve ≥80% branch coverage with 0 false positives; no hallucinated dependencies.

### Week 3: Active Pilot with Human-on-the-Loop
- **Objective**: Enable live processing for 10–15 real incoming tickets labeled `SpecDrivenDev`.
- **Workflow**:
  - Developers tag eligible tickets.
  - Reviewers receive Slack/Email alert for Gate 1 (tests-only MR) and approve in GitLab.
  - Agent completes code generation and CI validation.
  - Reviewers receive alert for Gate 2 (final MR) and either approve or request changes (testing FR-21 feedback loop).
- **Exit Criteria**: Reviewers successfully guide tickets to merge via GitLab with an average turnaround under 45 minutes.

### Week 4: Evaluation Against PRD Targets
Evaluate telemetry against PRD Section 8 Success Metrics:

| Metric | Target | Pilot Result |
|---|---|---|
| **Auto-Resolution Rate** | ≥ 70% of eligible tickets | TBD |
| **Turnaround Time** | < 45 min from ticket to PR-ready | TBD |
| **First-Pass PR Approval Rate** | ≥ 80% approved without rework | TBD |
| **Test Coverage Delta** | Maintained or improved, 0% regressions | TBD |
| **Tamper Detection Accuracy** | 100% detection | 100% (verified by unit test) |
| **Rate Limit Adherence** | ≤ 32 calls/min sustained | Verified |

---

## 2. Operational Runbooks

### Runbook 1: Resuming a Pipeline After Container Reboot
The pipeline saves full state to `checkpoints/<ticket-id>.json` after every stage and iteration.
- **CLI Mode**: Run `python -m orchestrator.main <TICKET_ID>`. The orchestrator will automatically load the checkpoint, restore the LLM conversation history, and resume at the interrupted stage.
- **Server Mode**: The background worker checks for existing checkpoints upon dequeuing and resumes seamlessly without repeating completed iterations.

### Runbook 2: Rate Limit Pause Handling
The in-process rate limiter (`rate_limiter.py`) caps calls at 32 per 60 seconds:
- If the cap is reached, the process automatically pauses and sleeps until the sliding window resets.
- Working context is retained in memory and checkpointed on disk.
- If persistent 429 errors occur, the LLM client automatically fails over to the configured secondary model (`deepseek-ai/deepseek-v4-pro`).

### Runbook 3: Rotating Secrets Without Downtime
To rotate API tokens (`JIRA_API_TOKEN`, `GITLAB_API_TOKEN`, `NIM_API_KEY`):
1. Update `.env` with the new token values.
2. If running under Docker Compose:
   ```bash
   docker compose restart orchestrator
   ```
3. Verify container health:
   ```bash
   curl -f http://localhost:8000/health
   ```

### Runbook 4: Emergency Rollback of an Automated PR
If an automated PR is found to cause an unexpected regression:
1. In GitLab: Close or revert the MR branch (`automation/<ticket-id>`).
2. In JIRA: Move ticket status back to "To Do" and remove label `SpecDrivenDev`.
3. To purge checkpoint cache:
   ```bash
   rm checkpoints/<ticket-id>.json
   ```
