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
