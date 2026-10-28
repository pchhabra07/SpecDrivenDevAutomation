"""
Centralised configuration loaded exclusively from environment variables.

Every setting required by the orchestrator is defined here as a Pydantic
BaseSettings model.  Secrets have no defaults and will raise a validation
error at startup if missing.  Non-secret tunables carry sensible defaults
that match the values agreed in the implementation plan.
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings
from typing_extensions import Self


class Settings(BaseSettings):
    """All orchestrator configuration — loaded from env vars."""

    # ── GitHub ──────────────────────────────────────────────────────────
    github_token: str = Field(
        "",
        description="GitHub Personal Access Token or GitHub App Token (secret)",
    )
    github_repo: str = Field(
        "",
        description="Target GitHub repository in 'owner/repo' format",
    )
    github_base_url: str = Field(
        "https://api.github.com",
        description="GitHub REST API base URL (override for GitHub Enterprise)",
    )
    github_webhook_secret: str = Field(
        "",
        description="Shared secret for validating incoming GitHub webhooks via HMAC-SHA256",
    )
    github_audit_repo: str = Field(
        "",
        description="GitHub repository in 'owner/repo' format for audit log shipping",
    )
    github_workflow_file: str = Field(
        "",
        description="Optional custom workflow file name in .github/workflows/ (defaults to <repo_name>-ci.yml)",
    )
    qualifying_label: str = Field(
        "SpecDrivenDev",
        description="Only tickets carrying this label are eligible for automation",
    )

    # ── NVIDIA NIM / LLM ───────────────────────────────────────────────
    nim_api_key: str = Field(..., description="NVIDIA NIM API key (secret)")
    nim_base_url: str = Field(..., description="NIM OpenAI-compatible base URL")
    nim_model: str = Field(
        "z-ai/glm-5.3",
        description="Primary model slug on NIM",
    )
    nim_fallback_model: str = Field(
        "deepseek-ai/deepseek-v4-pro",
        description="Fallback model after 3 consecutive primary failures",
    )

    # ── Rate limiting ───────────────────────────────────────────────────
    rate_limit_cap: int = Field(
        32,
        description="Max NIM calls per 60-second sliding window",
    )

    # ── Checkpointing & audit ──────────────────────────────────────────
    checkpoint_dir: str = Field(
        "./checkpoints",
        description="Directory for per-ticket checkpoint JSON files",
    )
    audit_dir: str = Field(
        "./audit",
        description="Directory for per-ticket audit JSONL logs",
    )

    # ── Code-generation loop ───────────────────────────────────────────
    max_code_iterations: int = Field(
        6,
        description="Max coding-loop iterations before escalation",
    )
    coverage_threshold: int = Field(
        80,
        description="Minimum Jest line-coverage percentage",
    )

    # ── CI/CD pipeline polling & Stage 4 validation ───────────────────
    ci_poll_interval_seconds: int = Field(
        30,
        description="Seconds between CI pipeline status polls",
    )
    ci_timeout_minutes: int = Field(
        30,
        description="Max minutes to wait for a CI pipeline run",
    )
    max_stage4_repair_attempts: int = Field(
        1,
        description="Maximum LLM repair attempts for Stage 4 gate failures before escalating",
    )
    reuse_stage3_final_pipeline: bool = Field(
        True,
        description="Reuse Stage 3 final full CI pipeline result in Stage 4 to avoid duplicate runs",
    )

    # ── SonarQube ──────────────────────────────────────────────────────
    sonar_host_url: str = Field("", description="SonarQube server URL")
    sonar_token: str = Field("", description="SonarQube auth token")

    # ── Human gate polling ─────────────────────────────────────────────
    gate_poll_interval_seconds: int = Field(
        30,
        description="Seconds between PR/MR approval polls",
    )
    gate_timeout_hours: float = Field(
        48.0,
        description="Hours before a gate poll times out",
    )

    # ── Escalation / notifications ─────────────────────────────────────
    slack_webhook_url: str = Field("", description="Slack incoming webhook URL")
    escalation_email: str = Field("", description="Email address for escalation alerts")

    # ── Webhook service (Phase 3) ──────────────────────────────────────
    webhook_host: str = Field("0.0.0.0", description="Host to bind FastAPI webhook service")
    webhook_port: int = Field(8000, description="Port to bind FastAPI webhook service")

    # ── Reviewer feedback loop (Phase 3) ───────────────────────────────
    max_review_iterations: int = Field(
        3,
        description="Max reviewer change-request iterations in Gate 2 before escalation",
    )

    # ── Audit shipping & SIEM (Phase 4) ────────────────────────────────
    audit_shipping_mode: str = Field(
        "none",
        description="Audit shipping mode: 'git', 'http', or 'none'",
    )
    siem_webhook_url: str = Field(
        "",
        description="HTTPS endpoint for SIEM audit log shipping when mode is 'http'",
    )

    # ── Concurrency & Scaling (Phase 5) ────────────────────────────────
    max_concurrent_tickets: int = Field(
        2,
        description="Number of concurrent worker tasks processing tickets in parallel",
    )
    repo_mapping: str = Field(
        "{}",
        description="JSON string mapping components/labels to target repository identifiers",
    )
    enable_context_pruning: bool = Field(
        False,
        description="Enable automatic sliding-window pruning for long LLM transcripts (default: False)",
    )
    max_context_tokens: int = Field(
        131072,
        description="Maximum estimated token threshold before older iterations are summarized",
    )

    # ── Stage 1 context limits (S1-4 fix) ─────────────────────────────
    max_context_test_files: int = Field(
        10,
        description=(
            "Maximum number of existing test files to include in the Stage 1 context. "
            "Test files are sorted by symbol relevance before this cap is applied."
        ),
    )

    # ── Stage 2 settings (S2-1, S2-4, S2-5 fixes) ────────────────────
    max_gate1_revision_cycles: int = Field(
        5,
        description="Maximum Gate 1 test-revision cycles before pipeline aborts",
    )
    test_preflight_max_repairs: int = Field(
        2,
        description="Maximum preflight repair attempts for generated tests in Stage 2",
    )
    blocking_gates: bool = Field(
        True,
        description="Wait synchronously for human gates (for CLI/dev use; set False for non-blocking server/webhook mode)",
    )

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def validate_github_credentials(self) -> Self:
        if self.github_repo:
            repo = self.github_repo.strip()
            if "github.com" in repo:
                repo = repo.split("github.com", 1)[1].lstrip("/:").removesuffix(".git")
            self.github_repo = repo
        if self.github_audit_repo:
            audit_repo = self.github_audit_repo.strip()
            if "github.com" in audit_repo:
                audit_repo = audit_repo.split("github.com", 1)[1].lstrip("/:").removesuffix(".git")
            self.github_audit_repo = audit_repo
        if not self.github_token or not self.github_repo:
            raise ValueError("GITHUB_TOKEN and GITHUB_REPO are required.")
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """
    Return the module-level Settings singleton, creating it on first call.

    Lazy instantiation ensures that importing this module during tests
    does not trigger validation errors when env vars are absent.
    """
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
