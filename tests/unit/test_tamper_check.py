"""
Unit tests for orchestrator.tamper_check.
"""

import pytest

from orchestrator.tamper_check import build_lock_hashes, check_tampering, hash_content


class TestHashContent:
    """Tests for the hash_content helper."""

    def test_deterministic(self):
        """Same input should always produce the same hash."""
        assert hash_content("hello") == hash_content("hello")

    def test_different_inputs_differ(self):
        """Different inputs should produce different hashes."""
        assert hash_content("hello") != hash_content("world")

    def test_bytes_and_str_match(self):
        """bytes and the equivalent str should hash identically."""
        assert hash_content("test") == hash_content(b"test")


class TestBuildLockHashes:
    """Tests for build_lock_hashes."""

    def test_builds_correct_mapping(self):
        files = {
            "src/foo.test.ts": "describe('foo', () => {})",
            "src/bar.test.ts": "describe('bar', () => {})",
        }
        hashes = build_lock_hashes(files)
        assert set(hashes.keys()) == set(files.keys())
        for path, content in files.items():
            assert hashes[path] == hash_content(content)

    def test_empty_input(self):
        assert build_lock_hashes({}) == {}


class TestCheckTampering:
    """Tests for check_tampering."""

    def test_no_tampering(self):
        """Unchanged files should return an empty list."""
        files = {"a.test.ts": "content A", "b.test.ts": "content B"}
        hashes = build_lock_hashes(files)
        result = check_tampering(hashes, files)
        assert result == []

    def test_detects_modified_file(self):
        """A file with changed content should be flagged."""
        original = {"a.test.ts": "original"}
        hashes = build_lock_hashes(original)
        modified = {"a.test.ts": "TAMPERED"}
        result = check_tampering(hashes, modified)
        assert result == ["a.test.ts"]

    def test_detects_deleted_file(self):
        """A file missing from current contents should be flagged."""
        original = {"a.test.ts": "content"}
        hashes = build_lock_hashes(original)
        result = check_tampering(hashes, {})
        assert result == ["a.test.ts"]

    def test_multiple_tampered_files(self):
        """Multiple tampered files should all be reported."""
        original = {
            "a.test.ts": "aaa",
            "b.test.ts": "bbb",
            "c.test.ts": "ccc",
        }
        hashes = build_lock_hashes(original)
        current = {
            "a.test.ts": "CHANGED",
            "b.test.ts": "bbb",       # untouched
            # c.test.ts deleted
        }
        result = check_tampering(hashes, current)
        assert "a.test.ts" in result
        assert "c.test.ts" in result
        assert "b.test.ts" not in result

    def test_whitespace_change_is_tampering(self):
        """Even a whitespace-only change should be detected."""
        original = {"a.test.ts": "line1\nline2"}
        hashes = build_lock_hashes(original)
        tweaked = {"a.test.ts": "line1\nline2\n"}
        result = check_tampering(hashes, tweaked)
        assert result == ["a.test.ts"]
