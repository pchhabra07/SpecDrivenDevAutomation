"""
Unit tests for orchestrator.metrics_engine (Phase 5).
"""

from pathlib import Path

import pytest

from orchestrator.metrics_engine import (
    compute_metrics,
    generate_json_summary,
    generate_markdown_report,
    parse_ticket_audit,
)


@pytest.fixture
def mock_audit_dir(tmp_path):
    # Ticket 1: Success on iteration 2, first-pass approval
    t1 = tmp_path / "PROJ-1.jsonl"
    t1.write_text(
        '{"timestamp": "2026-09-21T10:00:00", "event_type": "pipeline_start", "stage": 0}\n'
        '{"timestamp": "2026-09-21T10:05:00", "event_type": "stage1_start", "stage": 1}\n'
        '{"timestamp": "2026-09-21T10:10:00", "event_type": "gate1_approved", "stage": 2}\n'
        '{"timestamp": "2026-09-21T10:15:00", "event_type": "stage3_iteration", "stage": 3, "payload": {"iteration": 2}}\n'
        '{"timestamp": "2026-09-21T10:20:00", "event_type": "gate2_approved", "stage": 5}\n'
        '{"timestamp": "2026-09-21T10:20:00", "event_type": "pipeline_complete", "stage": 0}\n',
        encoding="utf-8",
    )

    # Ticket 2: Escalated at iteration 6
    t2 = tmp_path / "PROJ-2.jsonl"
    t2.write_text(
        '{"timestamp": "2026-09-21T11:00:00", "event_type": "pipeline_start", "stage": 0}\n'
        '{"timestamp": "2026-09-21T11:05:00", "event_type": "gate1_approved", "stage": 2}\n'
        '{"timestamp": "2026-09-21T11:25:00", "event_type": "stage3_iteration", "stage": 3, "payload": {"iteration": 6}}\n'
        '{"timestamp": "2026-09-21T11:30:00", "event_type": "stage3_escalation", "stage": 3}\n',
        encoding="utf-8",
    )

    # Ticket 3: Triaged ineligible
    t3 = tmp_path / "PROJ-3.jsonl"
    t3.write_text(
        '{"timestamp": "2026-09-21T12:00:00", "event_type": "pipeline_start", "stage": 0}\n'
        '{"timestamp": "2026-09-21T12:01:00", "event_type": "stage1_triaged_ineligible", "stage": 1}\n',
        encoding="utf-8",
    )

    # Ticket 4: Tamper detected
    t4 = tmp_path / "PROJ-4.jsonl"
    t4.write_text(
        '{"timestamp": "2026-09-21T13:00:00", "event_type": "pipeline_start", "stage": 0}\n'
        '{"timestamp": "2026-09-21T13:05:00", "event_type": "gate1_approved", "stage": 2}\n'
        '{"timestamp": "2026-09-21T13:10:00", "event_type": "stage3_tamper_detected", "stage": 3}\n',
        encoding="utf-8",
    )

    return tmp_path


class TestMetricsEngine:
    def test_parse_ticket_audit(self, mock_audit_dir):
        res = parse_ticket_audit(mock_audit_dir / "PROJ-1.jsonl")
        assert res is not None
        assert res["ticket_id"] == "PROJ-1"
        assert res["gate2_approved"] is True
        assert res["first_pass_approval"] is True
        assert res["coding_iterations"] == 2
        assert res["duration_seconds"] == 1200.0  # 20 minutes

    def test_compute_metrics(self, mock_audit_dir):
        m = compute_metrics(mock_audit_dir)

        assert m["total_tickets"] == 4
        assert m["eligible_tickets"] == 3  # PROJ-1, PROJ-2, PROJ-4
        assert m["ineligible_triaged"] == 1  # PROJ-3
        assert m["successful_tickets"] == 1  # PROJ-1
        assert m["auto_resolution_rate"] == 33.3  # 1/3
        assert m["first_pass_approval_rate"] == 100.0  # PROJ-1 had 0 feedback cycles
        assert m["tamper_incidents"] == 1  # PROJ-4
        assert m["escalations"] == 1  # PROJ-2

    def test_empty_audit_dir(self, tmp_path):
        m = compute_metrics(tmp_path / "nonexistent")
        assert m["total_tickets"] == 0
        assert m["auto_resolution_rate"] == 0.0

    def test_generate_markdown_report(self, mock_audit_dir, tmp_path):
        out = tmp_path / "report.md"
        report = generate_markdown_report(mock_audit_dir, output_path=out)

        assert "# Spec-Driven Development Automation — Pilot Evaluation Report" in report
        assert "PROJ-1" in report
        assert "33.3%" in report
        assert out.exists()
        assert len(out.read_text(encoding="utf-8")) > 200

    def test_generate_json_summary(self, mock_audit_dir):
        summary = generate_json_summary(mock_audit_dir)
        assert isinstance(summary, dict)
        assert summary["total_tickets"] == 4
        assert len(summary["ticket_summaries"]) == 4
