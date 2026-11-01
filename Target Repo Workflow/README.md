# Target Repo Workflow — Setup Instructions

This folder contains a GitHub Actions workflow that you add to **any target repository** you want the Spec-Driven Dev pipeline to automatically process.

## What It Does

When you add this workflow to a target repo, it will:
- **Trigger** whenever a new issue is opened or labeled in that repository.
- **Send a signed POST request** to your Spec-Driven pipeline server's webhook endpoint.
- The pipeline server picks up the issue, runs the full 5-stage AI pipeline, and creates PRs with generated tests and code directly in the target repository.

## Files to Copy

```
Target Repo Workflow/
└── .github/
    └── workflows/
        └── notify-spec-driven-pipeline.yml   ← copy this to your target repo
```

Also copy (from the pipeline repo root):

```
.github/workflows/spec-driven-dev-ci.yml      ← copy this to your target repo
```

The second file (`spec-driven-dev-ci.yml`) is the CI validation workflow that Stage 4 triggers to run ESLint, Semgrep, Trivy, and tests on the generated code.

## Setup Steps

1. Copy `.github/workflows/notify-spec-driven-pipeline.yml` into the target repository.
2. In the target repository, add two **Repository Secrets** (Settings → Secrets and variables → Actions):
   - `SPEC_PIPELINE_SERVER_URL` — the public URL of your pipeline server (e.g. `https://pipeline.example.com`)
   - `SPEC_PIPELINE_WEBHOOK_SECRET` — the same secret as `GITHUB_WEBHOOK_SECRET` in the pipeline's `.env`
3. Copy `.github/workflows/spec-driven-dev-ci.yml` into the target repository.
4. Label any issue with `SpecDrivenDev` to trigger the pipeline.

## Concurrency

The workflow uses `concurrency: cancel-in-progress: false` so that rapid issue creation is **queued, not dropped**. Combined with `MAX_CONCURRENT_TICKETS=1` on the server, only one pipeline run executes at a time.
