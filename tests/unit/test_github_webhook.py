"""
Unit tests for GitHub Webhook handling in webhook.app.
"""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from webhook.app import _recent_events, app, task_queue, verify_github_hmac

client = TestClient(app)


def _compute_hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body := body, hashlib.sha256).hexdigest()


class TestGitHubVerifyHMAC:
    def test_valid_signature(self):
        secret = "githubsecret"
        body = b'{"action": "opened"}'
        sig = f"sha256={_compute_hmac(secret, body)}"
        assert verify_github_hmac(body, sig, secret) is True

    def test_invalid_signature(self):
        secret = "githubsecret"
        body = b'{"action": "opened"}'
        assert verify_github_hmac(body, "sha256=invalidhash", secret) is False

    def test_missing_signature_with_secret_fails(self):
        assert verify_github_hmac(b"{}", None, "secret123") is False

    def test_no_secret_configured_allows_request(self):
        assert verify_github_hmac(b"{}", None, "") is True


class TestGitHubWebhookEndpoint:
    def setup_method(self):
        _recent_events.clear()
        while not task_queue.empty():
            task_queue.get_nowait()

    @patch("webhook.app.get_settings")
    def test_github_webhook_invalid_hmac_rejected(self, mock_settings):
        settings = MagicMock()
        settings.github_webhook_secret = "secret99"
        mock_settings.return_value = settings

        body = json.dumps({"action": "opened"}).encode()
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=wrong",
                "X-GitHub-Event": "issues",
            },
        )
        assert response.status_code == 401

    @patch("webhook.app.get_settings")
    def test_github_webhook_ping_event(self, mock_settings):
        settings = MagicMock()
        settings.github_webhook_secret = ""
        mock_settings.return_value = settings

        response = client.post(
            "/webhooks/github",
            json={"zen": "Non-blocking is better than blocking."},
            headers={"X-GitHub-Event": "ping"},
        )
        assert response.status_code == 202
        assert response.json()["status"] == "pong"

    @patch("webhook.app.get_settings")
    def test_github_webhook_qualifying_issue_enqueued(self, mock_settings):
        settings = MagicMock()
        settings.github_webhook_secret = "secret99"
        settings.github_repo = "org/my-service"
        settings.qualifying_label = "SpecDrivenDev"
        mock_settings.return_value = settings

        payload = {
            "action": "labeled",
            "repository": {"full_name": "org/my-service"},
            "issue": {
                "number": 77,
                "title": "Add feature",
                "labels": [{"name": "SpecDrivenDev"}, {"name": "enhancement"}],
                "updated_at": "2026-09-21T12:00:00Z",
            },
        }
        body = json.dumps(payload).encode()
        sig = f"sha256={_compute_hmac('secret99', body)}"

        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
            },
        )
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert data["ticket_id"] == "77"
        assert task_queue.qsize() == 1

    @patch("webhook.app.get_settings")
    def test_github_webhook_missing_qualifying_label(self, mock_settings):
        settings = MagicMock()
        settings.github_webhook_secret = ""
        settings.github_repo = "org/my-service"
        settings.qualifying_label = "SpecDrivenDev"
        mock_settings.return_value = settings

        payload = {
            "action": "opened",
            "repository": {"full_name": "org/my-service"},
            "issue": {
                "number": 88,
                "labels": [{"name": "bug"}],
            },
        }
        response = client.post(
            "/webhooks/github",
            json=payload,
            headers={"X-GitHub-Event": "issues"},
        )
        assert response.status_code == 202
        assert response.json()["status"] == "ignored"
        assert response.json()["reason"] == "missing_qualifying_label"
        assert task_queue.empty()

    @patch("webhook.app.get_settings")
    def test_github_webhook_deduplication(self, mock_settings):
        settings = MagicMock()
        settings.github_webhook_secret = ""
        settings.github_repo = "org/my-service"
        settings.qualifying_label = "SpecDrivenDev"
        mock_settings.return_value = settings

        payload = {
            "action": "opened",
            "repository": {"full_name": "org/my-service"},
            "issue": {
                "number": 99,
                "labels": [{"name": "SpecDrivenDev"}],
                "updated_at": "2026-09-21T12:00:00Z",
            },
        }

        r1 = client.post("/webhooks/github", json=payload, headers={"X-GitHub-Event": "issues"})
        assert r1.status_code == 202
        assert r1.json()["status"] == "queued"

        r2 = client.post("/webhooks/github", json=payload, headers={"X-GitHub-Event": "issues"})
        assert r2.status_code == 202
        assert r2.json()["status"] == "duplicate_ignored"
        assert task_queue.qsize() == 1
