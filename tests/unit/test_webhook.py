"""
Unit tests for webhook.app (FastAPI Webhook Service).
"""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from webhook.app import _recent_events, app, task_queue, verify_jira_hmac

client = TestClient(app)


def _compute_hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class TestVerifyHMAC:
    def test_valid_signature(self):
        secret = "supersecret"
        body = b'{"test": 123}'
        sig = f"sha256={_compute_hmac(secret, body)}"
        assert verify_jira_hmac(body, sig, secret) is True

    def test_invalid_signature(self):
        secret = "supersecret"
        body = b'{"test": 123}'
        assert verify_jira_hmac(body, "sha256=invalidhash", secret) is False

    def test_missing_signature_with_secret_fails(self):
        assert verify_jira_hmac(b"{}", None, "secret123") is False

    def test_no_secret_configured_allows_request(self):
        assert verify_jira_hmac(b"{}", None, "") is True


class TestWebhookEndpoints:
    def setup_method(self):
        _recent_events.clear()
        # Empty queue
        while not task_queue.empty():
            task_queue.get_nowait()

    @patch("webhook.app.get_settings")
    def test_jira_webhook_invalid_hmac_rejected(self, mock_settings):
        settings = MagicMock()
        settings.jira_webhook_secret = "mysecret"
        mock_settings.return_value = settings

        body = json.dumps({"test": "data"}).encode()
        response = client.post(
            "/webhooks/jira",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=wrong"},
        )
        assert response.status_code == 401

    @patch("webhook.app.get_settings")
    def test_jira_webhook_qualifying_ticket_enqueued(self, mock_settings):
        settings = MagicMock()
        settings.jira_webhook_secret = "mysecret"
        settings.jira_project_key = "PROJ"
        settings.qualifying_label = "SpecDrivenDev"
        mock_settings.return_value = settings

        payload = {
            "webhookEvent": "jira:issue_created",
            "timestamp": 1700000000,
            "issue": {
                "key": "PROJ-101",
                "fields": {
                    "project": {"key": "PROJ"},
                    "labels": ["SpecDrivenDev", "Bug"],
                    "status": {"name": "Ready for Automation"},
                },
            },
        }
        body = json.dumps(payload).encode()
        sig = f"sha256={_compute_hmac('mysecret', body)}"

        response = client.post(
            "/webhooks/jira",
            content=body,
            headers={"X-Hub-Signature-256": sig},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert data["ticket_id"] == "PROJ-101"
        assert task_queue.qsize() == 1

    @patch("webhook.app.get_settings")
    def test_jira_webhook_missing_label_ignored(self, mock_settings):
        settings = MagicMock()
        settings.jira_webhook_secret = ""
        settings.jira_project_key = "PROJ"
        settings.qualifying_label = "SpecDrivenDev"
        mock_settings.return_value = settings

        payload = {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": "PROJ-102",
                "fields": {
                    "project": {"key": "PROJ"},
                    "labels": ["General"],
                },
            },
        }

        response = client.post("/webhooks/jira", json=payload)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "ignored"
        assert data["reason"] == "missing_qualifying_label"
        assert task_queue.empty()

    @patch("webhook.app.get_settings")
    def test_jira_webhook_deduplication(self, mock_settings):
        settings = MagicMock()
        settings.jira_webhook_secret = ""
        settings.jira_project_key = "PROJ"
        settings.qualifying_label = "SpecDrivenDev"
        mock_settings.return_value = settings

        payload = {
            "webhookEvent": "jira:issue_created",
            "timestamp": 1700000555,
            "issue": {
                "key": "PROJ-103",
                "fields": {
                    "project": {"key": "PROJ"},
                    "labels": ["SpecDrivenDev"],
                },
            },
        }

        # First post -> queued
        r1 = client.post("/webhooks/jira", json=payload)
        assert r1.status_code == 202
        assert r1.json()["status"] == "queued"

        # Duplicate post -> ignored
        r2 = client.post("/webhooks/jira", json=payload)
        assert r2.status_code == 202
        assert r2.json()["status"] == "duplicate_ignored"
        assert task_queue.qsize() == 1

    def test_health_endpoint(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "worker" in data
        assert "queue_length" in data

    def test_metrics_endpoint(self):
        response = client.get("/metrics")
        assert response.status_code == 200
        text = response.text
        assert "spec_dev_queue_length" in text
        assert "spec_dev_tickets_processed_total" in text
