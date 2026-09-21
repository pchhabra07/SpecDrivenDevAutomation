"""
Centralised configuration loaded exclusively from environment variables.

Every setting required by the orchestrator is defined here as a Pydantic
BaseSettings model.  Secrets have no defaults and will raise a validation
error at startup if missing.  Non-secret tunables carry sensible defaults
that match the values agreed in the implementation plan.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All orchestrator configuration — loaded from env vars."""

    # ── JIRA ────────────────────────────────────────────────────────────
    jira_base_url: str = Field(..., description="JIRA instance root URL")
    jira_api_token: str = Field(..., description="JIRA API token (secret)")
    jira_user_email: str = Field(..., description="JIRA user email for basic auth")
    jira_project_key: str = Field(..., description="JIRA project key filter")
    qualifying_label: str = Field(
        "SpecDrivenDev",
        description="Only tickets carrying this label are eligible for automation",
    )

    # ── GitLab ──────────────────────────────────────────────────────────
    gitlab_base_url: str = Field(..., description="GitLab instance URL")
    gitlab_api_token: str = Field(..., description="GitLab personal/project token (secret)")
    gitlab_repo_id: int = Field(..., description="Target repo numeric ID")

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

    # ── CI/CD pipeline polling ─────────────────────────────────────────
    ci_poll_interval_seconds: int = Field(
        30,
        description="Seconds between CI pipeline status polls",
    )
    ci_timeout_minutes: int = Field(
        30,
        description="Max minutes to wait for a CI pipeline run",
    )

    # ── SonarQube ──────────────────────────────────────────────────────
    sonar_host_url: str = Field("", description="SonarQube server URL")
    sonar_token: str = Field("", description="SonarQube auth token")

    # ── Human gate polling ─────────────────────────────────────────────
    gate_poll_interval_seconds: int = Field(
        30,
        description="Seconds between GitLab MR approval polls",
    )
    gate_timeout_hours: float = Field(
        48.0,
        description="Hours before a gate poll times out",
    )

    # ── Escalation / notifications ─────────────────────────────────────
    slack_webhook_url: str = Field("", description="Slack incoming webhook URL")
    escalation_email: str = Field("", description="Email address for escalation alerts")

    # ── Webhook service (Phase 3) ──────────────────────────────────────
    jira_webhook_secret: str = Field(
        "",
        description="Shared secret for validating incoming JIRA webhooks via HMAC-SHA256",
    )
    webhook_host: str = Field("0.0.0.0", description="Host to bind FastAPI webhook service")
    webhook_port: int = Field(8000, description="Port to bind FastAPI webhook service")
    gitlab_webhook_secret: str = Field(
        "",
        description="Secret token to validate incoming GitLab webhooks",
    )

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
    audit_gitlab_repo_id: int | None = Field(
        None,
        description="GitLab repo ID to commit audit logs into when audit_shipping_mode is 'git'",
    )
    siem_webhook_url: str = Field(
        "",
        description="HTTPS endpoint for SIEM audit log shipping when mode is 'http'",
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


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
