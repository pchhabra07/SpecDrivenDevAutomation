"""Unit tests for orchestrator.ci_log_parser (Issue 2 fix)."""
from __future__ import annotations

import pytest

from orchestrator.ci_log_parser import (
    ParsedError,
    format_errors_for_prompt,
    parse_ci_log,
    _parse_pytest,
    _parse_jest,
    _parse_generic,
)


class TestParsePytest:
    def test_failed_line_extracted(self):
        log = "FAILED tests/test_auth.py::test_login - AssertionError: expected 401 got 200"
        errors = _parse_pytest(log)
        assert len(errors) >= 1
        e = errors[0]
        assert "test_auth.py" in e.file
        assert "test_login" in e.test_name
        assert e.error_type == "assertion"

    def test_import_error_extracted(self):
        log = "ImportError: No module named 'app.models'"
        errors = _parse_pytest(log)
        assert any("ImportError" in e.message for e in errors)
        assert any(e.error_type == "import" for e in errors)

    def test_file_line_extracted(self):
        log = "  src/auth.py:42: AssertionError"
        errors = _parse_pytest(log)
        assert any(e.line == 42 for e in errors)

    def test_empty_log_returns_empty(self):
        assert _parse_pytest("") == []


class TestParseJest:
    def test_jest_block_extracted(self):
        log = (
            "\u25cf test login redirects unauthenticated users\n"
            "\n"
            "  Expected: 'login'\n"
            "  Received: 'dashboard'\n"
            "\n"
            "  at Object.<anonymous> (src/router.test.ts:15:5)\n"
        )
        errors = _parse_jest(log)
        assert len(errors) >= 1
        e = errors[0]
        assert "login redirects" in e.test_name
        assert "Expected" in e.message or "Received" in e.message

    def test_empty_log_returns_empty(self):
        assert _parse_jest("") == []


class TestParseGeneric:
    def test_error_line_extracted(self):
        log = "Error in src/utils.py:88: division by zero"
        errors = _parse_generic(log)
        assert any("utils.py" in e.file for e in errors)

    def test_empty_log_returns_empty(self):
        assert _parse_generic("") == []


class TestParseCiLog:
    def test_tries_pytest_first(self):
        log = "FAILED tests/test_x.py::test_y - ValueError: bad"
        errors = parse_ci_log(log)
        assert len(errors) >= 1
        assert "test_x.py" in errors[0].file

    def test_deduplication(self):
        log = (
            "FAILED tests/test_x.py::test_y - AssertionError\n"
            "FAILED tests/test_x.py::test_y - AssertionError\n"
        )
        errors = parse_ci_log(log)
        # Should be deduped to 1
        duplicates = [e for e in errors if "test_x.py" in e.file]
        assert len(duplicates) == 1

    def test_cap_at_20(self):
        lines = "\n".join(
            f"FAILED tests/test_x.py::test_{i} - AssertionError: {i}"
            for i in range(30)
        )
        errors = parse_ci_log(lines)
        assert len(errors) <= 20

    def test_empty_log(self):
        assert parse_ci_log("") == []


class TestFormatErrorsForPrompt:
    def test_returns_empty_for_no_errors(self):
        assert format_errors_for_prompt([]) == ""

    def test_contains_file_and_message(self):
        errors = [
            ParsedError(file="src/auth.py", line=42, message="expected 401 got 200",
                        test_name="test_login", error_type="assertion")
        ]
        output = format_errors_for_prompt(errors)
        assert "src/auth.py" in output
        assert "expected 401 got 200" in output
        assert "test_login" in output
        assert "ASSERTION" in output

    def test_multiple_errors_numbered(self):
        errors = [
            ParsedError(file="a.py", line=1, message="err1", error_type="error"),
            ParsedError(file="b.py", line=2, message="err2", error_type="syntax"),
        ]
        output = format_errors_for_prompt(errors)
        assert "Error 1" in output
        assert "Error 2" in output
