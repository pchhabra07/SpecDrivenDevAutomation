"""
Unit tests for orchestrator.audit_shipper (Phase 4).
"""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.audit_shipper import (
    compute_file_sha256,
    ship_audit_log,
    ship_audit_log_to_git,
    ship_audit_log_to_siem,
)
from orchestrator.config import Settings


@pytest.fixture
def temp_audit_file(tmp_path):
    f = tmp_path / "PROJ-123.jsonl"
    f.write_text('{"event": "start", "ticket_id": "PROJ-123"}\n{"event": "complete"}\n', encoding="utf-8")
    return f


class TestAuditShipper:
    def test_compute_file_sha256(self, temp_audit_file):
        expected = hashlib.sha256(temp_audit_file.read_bytes()).hexdigest()
        digest = compute_file_sha256(temp_audit_file)
        assert digest == expected

    def test_compute_file_sha256_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            compute_file_sha256(tmp_path / "nonexistent.jsonl")

    def test_ship_audit_log_to_git(self, temp_audit_file):
        mock_client = MagicMock()
        mock_client.commit_files.return_value = "abcdef1234567890"

        commit_id = ship_audit_log_to_git(
            filepath=temp_audit_file,
            client=mock_client,
            audit_repo="org/audit-repo",
        )

        assert commit_id == "abcdef1234567890"
        mock_client.commit_files.assert_called_once()
        kwargs = mock_client.commit_files.call_args[1]
        assert "PROJ-123" in kwargs["commit_message"]
        assert len(kwargs["files"]) == 1

    @patch("httpx.post")
    def test_ship_audit_log_to_siem_success(self, mock_post, temp_audit_file):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        success = ship_audit_log_to_siem(
            filepath=temp_audit_file,
            siem_url="https://siem.test/api/events",
            api_token="secret-token",
        )

        assert success is True
        mock_post.assert_called_once()
        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer secret-token"

    @patch("httpx.post")
    def test_ship_audit_log_to_siem_failure(self, mock_post, temp_audit_file):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        success = ship_audit_log_to_siem(
            filepath=temp_audit_file,
            siem_url="https://siem.test/api/events",
        )

        assert success is False

    def test_ship_audit_log_local_mode(self, temp_audit_file, tmp_path):
        settings = Settings(
            github_token="ghp_test",
            github_repo="org/repo",
            nim_api_key="token",
            nim_base_url="https://nim.test",
            audit_shipping_mode="none",
        )

        res = ship_audit_log("PROJ-123", str(tmp_path), settings)
        assert res["status"] == "local_only"
        assert len(res["sha256"]) == 64

    def test_ship_audit_log_missing_file(self, tmp_path):
        settings = Settings(
            github_token="ghp_test",
            github_repo="org/repo",
            nim_api_key="token",
            nim_base_url="https://nim.test",
            audit_shipping_mode="none",
        )

        res = ship_audit_log("MISSING-TICKET", str(tmp_path), settings)
        assert res["status"] == "skipped"
