"""
Unit tests for orchestrator.checkpoint.
"""

import json
import os
import tempfile

import pytest

from orchestrator.checkpoint import Checkpoint


class TestCheckpoint:
    """Tests for checkpoint save/load/delete."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_save_and_load_roundtrip(self):
        """A checkpoint saved to disk should load back identically."""
        cp = Checkpoint(
            ticket_id="PROJ-42",
            current_stage=2,
            iteration_count=3,
            locked_test_hashes={"src/foo.test.ts": "abc123"},
            modified_files=["src/foo.ts"],
            last_test_results={"passed": 3, "failed": 1},
        )
        cp.save(self._tmpdir)

        loaded = Checkpoint.load("PROJ-42", self._tmpdir)
        assert loaded is not None
        assert loaded.ticket_id == "PROJ-42"
        assert loaded.current_stage == 2
        assert loaded.iteration_count == 3
        assert loaded.locked_test_hashes == {"src/foo.test.ts": "abc123"}
        assert loaded.modified_files == ["src/foo.ts"]
        assert loaded.last_test_results == {"passed": 3, "failed": 1}

    def test_load_nonexistent_returns_none(self):
        """Loading a nonexistent checkpoint should return None."""
        assert Checkpoint.load("NOPE-999", self._tmpdir) is None

    def test_delete_removes_file(self):
        """delete() should remove the checkpoint file."""
        cp = Checkpoint(ticket_id="DEL-1")
        cp.save(self._tmpdir)
        filepath = os.path.join(self._tmpdir, "DEL-1.json")
        assert os.path.exists(filepath)

        Checkpoint.delete("DEL-1", self._tmpdir)
        assert not os.path.exists(filepath)

    def test_delete_nonexistent_is_noop(self):
        """delete() on a missing file should not raise."""
        Checkpoint.delete("NOPE-999", self._tmpdir)  # should not raise

    def test_save_overwrites_existing(self):
        """Saving twice to the same ticket should overwrite cleanly."""
        cp1 = Checkpoint(ticket_id="OVR-1", current_stage=1)
        cp1.save(self._tmpdir)

        cp2 = Checkpoint(ticket_id="OVR-1", current_stage=3, iteration_count=5)
        cp2.save(self._tmpdir)

        loaded = Checkpoint.load("OVR-1", self._tmpdir)
        assert loaded is not None
        assert loaded.current_stage == 3
        assert loaded.iteration_count == 5

    def test_conversation_history_persists(self):
        """The full conversation history should survive a save/load cycle."""
        history = [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Fix the bug."},
            {"role": "assistant", "content": "I'll look into it."},
        ]
        cp = Checkpoint(ticket_id="HIST-1", conversation_history=history)
        cp.save(self._tmpdir)

        loaded = Checkpoint.load("HIST-1", self._tmpdir)
        assert loaded is not None
        assert loaded.conversation_history == history

    def test_file_is_valid_json(self):
        """The saved file should be valid, human-readable JSON."""
        cp = Checkpoint(ticket_id="JSON-1", current_stage=2)
        cp.save(self._tmpdir)
        filepath = os.path.join(self._tmpdir, "JSON-1.json")
        with open(filepath) as fh:
            data = json.load(fh)
        assert data["ticket_id"] == "JSON-1"
        assert data["current_stage"] == 2
