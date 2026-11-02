# Product Requirements Document (PRD)
# Automated Ticket-to-PR Pipeline: JIRA → AI Coding Agent → GitLab PR

## 1. Overview

This project builds an automated pipeline that takes a JIRA ticket (bug or improvement), triggers an AI coding agent to analyze the relevant GitLab repository, generates and validates test cases from the ticket's specification, implements the required code changes, verifies the changes against tests and quality gates, and raises a GitLab pull request for human review and merge.

The approach follows **spec-driven development**: the ticket's acceptance criteria drive test generation *before* code is written, rather than tests being derived from the code after the fact.

## 2. Problem Statement

Currently, routine bug fixes and small improvements require a developer to manually:
- Read and interpret the JIRA ticket
- Locate the relevant code in the repository
- Write or update tests
- Implement the fix
- Validate it locally
- Raise and shepherd a PR through review

This is repetitive for a large share of tickets (small bugs, well-scoped improvements) and consumes engineering time that could go toward higher-complexity work. The goal is to automate the mechanical middle of this process — from ticket to a review-ready PR — while keeping humans firmly in control of what gets approved and merged.

## 3. Goals

- Reduce manual engineering effort spent on well-scoped bug fixes and small improvements.
- Ensure every automated code change is backed by tests derived from the ticket's actual requirements, not just the code that was written.
- Maintain code quality and security standards equal to or higher than the current manual process.
- Keep humans as the final authority at key decision points — this is a human-on-the-loop system, not a fully autonomous one.
- Produce a full audit trail from ticket to merged PR.

## 4. Non-Goals / Out of Scope (initial version)

- Fully autonomous merging without human review.
- Handling large, ambiguous, or architecturally significant tickets (initial scope is well-defined bugs/improvements with clear acceptance criteria).
- Replacing human QA/code review entirely — this augments, not replaces, review.
- Multi-repository or cross-service changes in the first version.
- Concurrent processing of multiple JIRA tickets in parallel — the initial version processes one ticket at a time, given free-tier LLM rate limits; concurrent processing is deferred until dedicated infrastructure (or a paid inference tier) is in place.

## 5. User Roles

| Role | Description |
|---|---|
| Ticket Reporter | Raises the JIRA ticket with a description and acceptance criteria. |
| Reviewer/Approver | Developer or QA engineer who reviews proposed tests (Gate 1) and the final PR (Gate 2). |
| Platform/DevOps Owner | Maintains the CI/CD pipeline, agent permissions, and sandboxing infrastructure. |

## 6. Core Requirements

### 6.1 Trigger
- **FR-1:** The system must trigger automatically when a JIRA ticket is created or transitioned to a defined status/label (e.g., "Ready for Automation").
- **FR-2:** The trigger must pass ticket ID, description, and acceptance criteria to the pipeline via webhook.

### 6.2 Context Assembly
- **FR-3:** The agent must retrieve the relevant repository, its current codebase, and its existing test suite before taking any action.
- **FR-4:** The agent must resolve which repository/module is relevant to the ticket (via ticket metadata, labels, or repo mapping).

### 6.3 Test Generation (Pre-Development)
- **FR-5:** The agent must generate new test cases, or flag existing ones for modification, based solely on the ticket's specification and acceptance criteria — **before** any code changes are made.
- **FR-6:** The system must support generation of unit tests, integration tests, and regression tests where applicable.
- **FR-7:** Proposed tests must be presented to a human reviewer for approval before proceeding (**Human Approval Gate 1**).
- **FR-8:** Once approved, tests must be locked (committed to the repo, with the agent's write access to test files restricted for the subsequent coding stage).

### 6.4 Code Generation
- **FR-9:** The agent must implement code changes iteratively, running the locked test suite after each iteration and reporting pass/fail counts and details.
- **FR-10:** The agent must **not** be able to modify locked test files during the code-generation loop.
- **FR-11:** After each iteration, the system must diff test files against the approved/locked version. Any unauthorized modification must halt the loop and flag it for human review rather than allowing a false "pass."

### 6.5 Validation (CI/CD Gates)
- **FR-12:** Once target tests pass, the pipeline must run the full existing test suite (regression check).
- **FR-13:** The pipeline must run linting and static analysis.
- **FR-14:** The pipeline must run security and dependency scanning.
- **FR-15:** The pipeline must verify code coverage is maintained or improved against a defined threshold.
- **FR-16:** All gate results must be logged for audit purposes.

### 6.6 Pull Request Creation
- **FR-17:** A PR must be created automatically only after all validation gates pass.
- **FR-18:** The PR must include: code changes, new/updated test cases, a report of actual test execution output, and validation gate results.
- **FR-19:** The PR must link back to the originating JIRA ticket.

### 6.7 Human Review & Merge
- **FR-20:** A human reviewer must approve the PR before merge (**Human Approval Gate 2**). No auto-merge.
- **FR-21:** If changes are requested, the pipeline must route back to the code-generation stage with reviewer feedback.

### 6.8 Security & Governance
- **FR-22:** All agent execution must occur in a sandboxed, ephemeral environment (container or microVM) with no persistent access beyond the pipeline run.
- **FR-23:** Credentials/secrets must be masked/short-lived and never exposed in agent context or logs.
- **FR-24:** The agent's tool permissions must be scoped to the minimum required (e.g., read-only repo access during analysis, restricted write access during coding, no write access to test files post-lock).
- **FR-25:** Every pipeline run must produce an audit trail linking ticket → generated tests → code changes → validation results → PR → merge decision.

### 6.9 LLM Provider & Rate-Limit Handling
- **FR-26:** The initial version uses NVIDIA NIM's free-tier inference API as the LLM provider, with GLM-5.2 as the primary model for agentic coding/tool-calling tasks, and DeepSeek V4 Pro or NVIDIA Nemotron 3 Super 120B available as alternates for comparison or fallback.
- **FR-27:** The system must process one JIRA ticket at a time (serial processing) in the initial version, to operate within the free tier's per-minute rate limit (~40 requests/minute).
- **FR-28:** The orchestrator must cap outgoing LLM calls to a safe margin below the provider's rate limit (~30–35 calls/minute) using an in-process counter, pausing new calls once the cap is reached and resuming automatically once the per-minute window resets.
- **FR-29:** Because the LLM API is stateless, the orchestrator must retain the full working context (conversation/tool-call history, files edited so far, current test results, iteration count) in memory for the ticket being processed, so that pausing for rate-limit resets does not lose any progress or require re-deriving context.
- **FR-30:** Ticket-processing state must be checkpointed to disk after each iteration, so that an unexpected process restart (independent of rate-limit pauses) does not require restarting the ticket from scratch.

## 7. Proposed Architecture (Summary)

The system is composed of five major processes:

1. **Assemble Ticket Context** — pulls ticket data and existing codebase/test suite.
2. **Generate Test Cases** — drafts tests from spec, routes through Human Approval Gate 1, locks approved tests.
3. **Generate & Iterate Code** — coding loop validated against locked tests, with tamper-checking.
4. **Validate Changes** — CI/CD quality gates (tests, lint, security, coverage).
5. **Manage Pull Request & Merge** — PR creation, Human Approval Gate 2, merge.

Underlying platform: GitLab CI/CD as the execution substrate, using the Codex/Copilot Agent SDK (or Claude Code Agent SDK) as the coding agent runtime, triggered via JIRA webhooks, with NVIDIA NIM (free tier) as the LLM inference provider in the initial version.

## 8. Success Metrics

| Metric | Target (initial) |
|---|---|
| % of eligible tickets successfully auto-resolved to a mergeable PR | Baseline TBD after pilot |
| Reduction in average time from ticket creation to PR-ready | TBD |
| % of generated PRs approved without major rework | TBD |
| Test coverage delta on automated PRs | Maintained or improved, 0% regressions |
| Incidents of test-tampering caught by tamper-check | 100% detection (by design) |
| Human approval gate turnaround time | TBD |
| Average LLM calls consumed per ticket (within rate-limit budget) | ≤ 30–35 calls/minute sustained, no dropped context on pause |

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Agent "games" tests instead of fixing code (e.g., weakening assertions, removing checks) | Lock tests pre-coding; diff-check after every iteration; halt and flag on tampering |
| Agent hallucinates incorrect fixes that pass shallow tests | Human review gates at test-approval and pre-merge stages; coverage and regression checks |
| Prompt injection via malicious ticket content or compromised dependencies | Sandboxed, ephemeral execution environment; scoped tool permissions |
| Credential leakage | Masked/short-lived CI variables; secrets never in agent context |
| Scope creep to complex/ambiguous tickets | Restrict initial rollout to well-scoped bug/improvement tickets with clear acceptance criteria |
| Reviewer fatigue / rubber-stamping approvals | Ensure PR includes real test output and gate results, not just pass/fail summaries, to support meaningful review |
| Free-tier LLM rate limits (~40 RPM) constrain throughput and prevent concurrent ticket processing | Serial, single-ticket processing with an in-process rate limiter (cap ~30–35 calls/minute) that pauses and resumes on quota reset; revisit with a paid tier or dedicated infra when concurrent processing is needed |
| Long-running tickets with many iterations approach the model's context window limit (since the full transcript is re-sent on every call) | Use large-context models (GLM-5.2 / Nemotron Super, up to 1M context); plan for pruning/summarizing older tool outputs if a ticket's transcript grows very large |

## 10. Open Questions

- Which ticket types/labels qualify for automation in the initial pilot?
- What coverage threshold and quality gate tools will be standardized (e.g., Semgrep, Snyk, SonarQube, Trivy)?
- What is the escalation path when the agent cannot resolve a ticket after N iterations?
- At what ticket volume does serial single-ticket processing become a bottleneck requiring concurrent-processing infrastructure or a paid inference tier?

## 11. Milestones (Proposed)

| Phase | Description |
|---|---|
| Phase 1 | Proof-of-concept: single repo, manual trigger, test generation + approval gate only |
| Phase 2 | Add iterative code generation with tamper-check and CI validation gates |
| Phase 3 | Full JIRA webhook automation + PR creation + merge gate |
| Phase 4 | Pilot rollout on a defined set of low-risk repos/ticket types |
| Phase 5 | Evaluate metrics, expand scope; assess need for concurrent ticket processing and/or a paid LLM tier |
