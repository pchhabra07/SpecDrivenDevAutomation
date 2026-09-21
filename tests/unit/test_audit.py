"""
Unit tests for orchestrator.audit.
"""

import json
import tempfile

import pytest

from orchestrator.audit import AuditLogger


class TestAuditLogger:
    """Tests for the append-only JSONL audit log."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_log_creates_file(self):
        """First log call should create the JSONL file."""
        logger = AuditLogger("PROJ-1", self._tmpdir)
        logger.log(stage=1, event_type="test_event")
        assert logger.filepath.exists()

    def test_entries_are_valid_jsonl(self):
        """Each line in the log should be valid JSON."""
        logger = AuditLogger("PROJ-2", self._tmpdir)
        logger.log(stage=1, event_type="event_a", payload={"key": "value"})
        logger.log(stage=2, event_type="event_b")

        with open(logger.filepath) as fh:
            lines = fh.readlines()
        assert len(lines) == 2
        for line in lines:
            data = json.loads(line)
            assert "timestamp" in data
            assert "ticket_id" in data
            assert "stage" in data
            assert "event_type" in data

    def test_read_all_returns_entries(self):
        """read_all() should return all logged entries."""
        logger = AuditLogger("PROJ-3", self._tmpdir)
        logger.log(stage=1, event_type="start")
        logger.log(stage=1, event_type="end")
        entries = logger.read_all()
        assert len(entries) == 2
        assert entries[0]["event_type"] == "start"
        assert entries[1]["event_type"] == "end"

    def test_read_all_empty_file(self):
        """read_all() on a fresh logger (no logs) should return empty list."""
        logger = AuditLogger("PROJ-4", self._tmpdir)
        assert logger.read_all() == []

    def test_payload_is_recorded(self):
        """Custom payload data should be persisted."""
        logger = AuditLogger("PROJ-5", self._tmpdir)
        logger.log(stage=3, event_type="iteration", payload={
            "passed": 5, "failed": 1, "files": ["a.ts", "b.ts"],
        })
        entries = logger.read_all()
        assert entries[0]["payload"]["passed"] == 5
        assert entries[0]["payload"]["files"] == ["a.ts", "b.ts"]

    def test_ticket_id_in_every_entry(self):
        """Every entry should contain the ticket ID."""
        logger = AuditLogger("ABC-99", self._tmpdir)
        logger.log(stage=1, event_type="x")
        entries = logger.read_all()
        assert entries[0]["ticket_id"] == "ABC-99"

    def test_append_only(self):
        """Subsequent calls should append, not overwrite."""
        logger = AuditLogger("APPEND-1", self._tmpdir)
        logger.log(stage=1, event_type="first")

        # Create a second logger instance for the same ticket.
        logger2 = AuditLogger("APPEND-1", self._tmpdir)
        logger2.log(stage=2, event_type="second")

        entries = logger2.read_all()
        assert len(entries) == 2
