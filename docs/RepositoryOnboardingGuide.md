# Target Repository Onboarding & Sandboxing Guide (Phase 4)

This guide details the requirements, permissions, and step-by-step procedures for onboarding a target repository into the **Spec-Driven Development Automation Pipeline**.

---

## 1. Repository Eligibility Criteria

To qualify for automated ticket-to-PR processing, target repositories must meet the following criteria:

| Criterion | Requirement | Verification Method |
|---|---|---|
| **Tech Stack** | JavaScript, TypeScript, React, Redux, Node.js | Inspect `package.json` |
| **Test Runner** | Jest with Istanbul LCOV coverage reporting (`--coverage`) | Run `npx jest --coverage` |
| **Static Analysis** | ESLint with JSON format output | Run `npx eslint . --format json` |
| **Modular Structure** | Tests separated from source files (e.g. `tests/`, `*.test.ts`) | Verify test glob patterns |
| **Protected Main** | Default branch protected from direct pushes | Check GitLab project settings |

---

## 2. Shared CI/CD Template Integration

Target repositories must include or inherit the standardized CI pipeline template:

In the target repository's `.gitlab-ci.yml`:
```yaml
include:
  - project: 'automation/spec-driven-pipeline'
    ref: 'main'
    file: '/ci_templates/gitlab-ci-template.yml'
```

This template defines the validation stages required by Stage 4:
1. `lint` (ESLint)
2. `sast` (Semgrep)
3. `dependency-scan` (Trivy)
4. `test-coverage` (Jest)
5. `quality-gate` (SonarQube scanner)

---

## 3. Bot Account & Principle of Least Privilege

To safeguard repositories against unauthorized alterations, the bot user (`spec-driven-bot`) must operate with scoped permissions:

### 3.1 GitLab Permissions
- **Project Role**: `Developer` (NOT Maintainer or Owner).
- **Allowed Actions**:
  - Read repository files and commit trees.
  - Create branches matching `automation/*`.
  - Push commits to `automation/*` branches.
  - Create and update merge requests.
  - Trigger CI/CD pipelines on `automation/*`.
- **Restricted Actions**:
  - Cannot push directly to default branch (`main` / `master`).
  - Cannot approve Merge Requests.
  - Cannot merge Pull Requests (merging is reserved for human approvers per FR-20).

### 3.2 Branch Protection Rules
In GitLab Project Settings (`Settings` → `Repository` → `Protected Branches`):
- **Branch**: `main` (or default branch)
- **Allowed to push**: `No one`
- **Allowed to merge**: `Maintainers`
- **Code Owner approval**: Required

---

## 4. Onboarding Checklist

1. [ ] **Verify Test Runner**: Confirm `npx jest` executes cleanly and outputs coverage to `coverage/lcov.info`.
2. [ ] **Add Bot User**: Add `spec-driven-bot` with `Developer` role in Project Members.
3. [ ] **Update `.gitlab-ci.yml`**: Add the `include:` directive referencing `ci_templates/gitlab-ci-template.yml`.
4. [ ] **Configure SonarQube Project**: Register the repository in SonarQube Community Edition and add `SONAR_PROJECT_KEY` to CI variables.
5. [ ] **Verify Webhook Connectivity**: Ensure JIRA and GitLab webhooks can reach the orchestrator endpoint (`POST https://orchestrator.domain/webhooks/jira`).
6. [ ] **Smoke Test**: Tag a low-risk bug ticket in JIRA with `SpecDrivenDev` and observe the automated pipeline run.
