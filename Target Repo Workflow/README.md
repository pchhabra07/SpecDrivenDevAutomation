# Target Repo Workflow — GitLab & JIRA Setup Instructions

This folder contains the target repository configuration for the **GitLab + JIRA** version of the Spec-Driven Development Pipeline.

---

## 1. CI Validation Workflow (`.gitlab-ci.yml`)

Copy `Target Repo Workflow/.gitlab-ci.yml` into the root of your target GitLab repository as `.gitlab-ci.yml`.

```
your-gitlab-target-repo/
└── .gitlab-ci.yml
```

This workflow defines the stages executed by the orchestrator:
- `test`: Unit tests execution (Jest or Pytest).
- `lint`: ESLint quality checks.
- `sast`: Semgrep static security testing.
- `dependency-scan`: Trivy CVE vulnerability scan.
- `test-coverage`: Code coverage verification.
- `quality-gate`: SonarQube quality gate analysis (if configured).

---

## 2. GitLab Webhook Configuration

In your target GitLab repository, go to **Settings → Webhooks → Add new webhook**:

1. **URL:** `https://your-pipeline-server.example.com/webhooks/gitlab`
2. **Secret token:** Same as `GITLAB_WEBHOOK_SECRET` in `.env`.
3. **Trigger events:**
   - ✅ **Merge request events**
   - ✅ **Comments**
4. Click **Add webhook**.

This allows human reviewers' MR approvals or `/approve` comments on GitLab to re-enqueue and advance the pipeline.

---

## 3. JIRA Webhook Configuration

In your JIRA instance, go to **Jira Settings → System → Webhooks → Create a Webhook**:

1. **URL:** `https://your-pipeline-server.example.com/webhooks/jira`
2. **Secret:** Same as `JIRA_WEBHOOK_SECRET` in `.env`.
3. **Issue events:**
   - ✅ **created**
   - ✅ **updated**
4. **JQL filter (optional):** `labels = "SpecDrivenDev"`
5. Click **Save**.

When an issue with the qualifying label `SpecDrivenDev` is created or labeled in JIRA, the webhook server enqueues the ticket for processing.
