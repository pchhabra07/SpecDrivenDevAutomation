"""
Escalation notifications — Slack and email.

Used exclusively by the escalation path in Stage 3 when the agent
cannot resolve a ticket after N iterations.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def notify_slack(webhook_url: str, message: str) -> bool:
    """
    Send a notification to a Slack incoming webhook.

    Returns True if the message was accepted (HTTP 200), False otherwise.
    """
    if not webhook_url:
        logger.warning("Slack webhook URL not configured — skipping notification.")
        return False

    try:
        resp = httpx.post(
            webhook_url,
            json={"text": message},
            timeout=10.0,
        )
        if resp.status_code == 200:
            logger.info("Slack notification sent.")
            return True
        logger.warning("Slack returned HTTP %d: %s", resp.status_code, resp.text)
        return False
    except httpx.HTTPError as exc:
        logger.error("Slack notification failed: %s", exc)
        return False


def notify_email(
    to: str,
    subject: str,
    body: str,
    *,
    smtp_host: str = "localhost",
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_password: str = "",
    from_addr: str = "spec-driven-pipeline@noreply.local",
) -> bool:
    """
    Send a plain-text email notification.

    Returns True if the message was sent successfully, False otherwise.
    In Phase 1 this is best-effort — if SMTP is not configured the
    notification is logged and skipped.
    """
    if not to:
        logger.warning("Escalation email not configured — skipping notification.")
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            if smtp_user and smtp_password:
                server.starttls()
                server.login(smtp_user, smtp_password)
            server.sendmail(from_addr, [to], msg.as_string())
        logger.info("Email notification sent to %s", to)
        return True
    except Exception as exc:
        logger.error("Email notification failed: %s", exc)
        return False


def build_escalation_message(
    ticket_id: str,
    iterations: int,
    test_results: dict[str, Any],
    blocker_assessment: str,
    mr_url: str,
) -> str:
    """
    Build a human-readable escalation summary used in both Slack and
    JIRA comments.
    """
    passed = test_results.get("passed", 0)
    failed = test_results.get("failed", 0)
    total = passed + failed

    return (
        f"⚠️  **Automated resolution failed for {ticket_id}**\n\n"
        f"The spec-driven pipeline attempted {iterations} iterations but "
        f"could not get all target tests to pass.\n\n"
        f"**Test results (last iteration):** {passed}/{total} passed, "
        f"{failed} failing\n\n"
        f"**Agent's assessment of blocker:**\n{blocker_assessment}\n\n"
        f"**Draft PR:** {mr_url}\n\n"
        f"Please continue this ticket manually."
    )


def _platform_names(url: str) -> tuple[str, str]:
    """Return (platform_name, pr_type_name) based on URL."""
    if "github" in (url or "").lower():
        return "GitHub", "Pull Request"
    return "GitLab", "Merge Request"


def notify_gate1_pending(
    slack_webhook_url: str,
    escalation_email: str,
    ticket_id: str,
    mr_url: str,
    test_count: int,
) -> None:
    """Notify reviewer that Gate 1 tests are generated and ready for approval."""
    plat, pr_name = _platform_names(mr_url)
    msg = (
        f"📝 *Gate 1 Review Required — {ticket_id}*\n"
        f"The Spec-Driven Development agent generated {test_count} test file(s) "
        f"derived from the ticket's acceptance criteria.\n"
        f"Please review and approve the tests on {plat}: {mr_url}"
    )
    if slack_webhook_url:
        notify_slack(slack_webhook_url, msg)
    if escalation_email:
        notify_email(escalation_email, f"[Gate 1] Tests Ready for Review: {ticket_id}", msg)


def notify_gate2_pending(
    slack_webhook_url: str,
    escalation_email: str,
    ticket_id: str,
    mr_url: str,
    gate_results_summary: str,
) -> None:
    """Notify reviewer that code changes and validation gates passed and MR/PR is ready."""
    plat, pr_name = _platform_names(mr_url)
    msg = (
        f"🚀 *Gate 2 Review Required — {ticket_id}*\n"
        f"The automated pipeline has completed code generation and all CI validation gates passed!\n\n"
        f"*Quality Gates:*\n{gate_results_summary}\n\n"
        f"*{plat} {pr_name}:* {mr_url}\n"
        f"Please review the PR and approve in {plat}."
    )
    if slack_webhook_url:
        notify_slack(slack_webhook_url, msg)
    if escalation_email:
        notify_email(escalation_email, f"[Gate 2] PR Ready for Review: {ticket_id}", msg)


def notify_gate2_approved(
    slack_webhook_url: str,
    escalation_email: str,
    ticket_id: str,
    mr_url: str,
    approved_by: list[str],
) -> None:
    """Notify team that Gate 2 MR/PR was approved by a human reviewer."""
    plat, pr_name = _platform_names(mr_url)
    users_str = ", ".join(f"@{u}" for u in approved_by) if approved_by else "reviewer"
    msg = (
        f"✅ *{pr_name} Approved — {ticket_id}*\n"
        f"{plat} {pr_name} {mr_url} has been approved by {users_str}!\n"
        f"Ready for human merge in {plat}."
    )
    if slack_webhook_url:
        notify_slack(slack_webhook_url, msg)
    if escalation_email:
        notify_email(escalation_email, f"[Approved] {ticket_id} {pr_name} Approved", msg)


def notify_feedback_received(
    slack_webhook_url: str,
    escalation_email: str,
    ticket_id: str,
    mr_url: str,
    feedback_notes: list[str],
) -> None:
    """Notify that reviewer requested changes on Gate 2 MR/PR."""
    plat, pr_name = _platform_names(mr_url)
    notes_preview = "\n- ".join(feedback_notes[:3])
    msg = (
        f"🔄 *Reviewer Feedback Received — {ticket_id}*\n"
        f"Reviewer requested changes on {plat} {pr_name}: {mr_url}\n\n"
        f"*Feedback summary:*\n- {notes_preview}\n\n"
        f"The agent is re-entering Stage 3 to apply corrections."
    )
    if slack_webhook_url:
        notify_slack(slack_webhook_url, msg)
    if escalation_email:
        notify_email(escalation_email, f"[Changes Requested] {ticket_id}", msg)


def notify_pipeline_complete(
    slack_webhook_url: str,
    escalation_email: str,
    ticket_id: str,
    mr_url: str,
) -> None:
    """Notify team that ticket pipeline has completed successfully."""
    plat, pr_name = _platform_names(mr_url)
    msg = (
        f"🎉 *Pipeline Complete — {ticket_id}*\n"
        f"All spec-driven development stages completed for {ticket_id}.\n"
        f"{plat} {pr_name}: {mr_url}"
    )
    if slack_webhook_url:
        notify_slack(slack_webhook_url, msg)
    if escalation_email:
        notify_email(escalation_email, f"[Complete] {ticket_id} Pipeline Finished", msg)

