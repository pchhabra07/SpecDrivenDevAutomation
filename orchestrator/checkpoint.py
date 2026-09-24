"""
Per-ticket checkpoint persistence.

Writes / reads a JSON file per ticket ID so that an unexpected process
restart can resume from the last completed stage or iteration without
re-running earlier work.

Implements FR-30.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """Serialisable state for a single ticket's pipeline run."""

    ticket_id: str
    current_stage: int = 1
    iteration_count: int = 0
    locked_test_hashes: dict[str, str] = field(default_factory=dict)
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    last_test_results: dict[str, Any] = field(default_factory=dict)
    gate1_mr_iid: int | None = None
    draft_mr_iid: int | None = None
    gate1_approved: bool = False
    tests_branch: str = ""
    code_branch: str = ""
    ci_pipeline_id: int | None = None
    validation_result: dict[str, Any] = field(default_factory=dict)
    gate2_mr_iid: int | None = None
    gate2_approved: bool = False
    review_iteration_count: int = 0
    reviewer_feedback: list[str] = field(default_factory=list)
    final_mr_url: str = ""
    repo_id: str | int | None = None
    ci_workflow_path: str = ""

    # ── Persistence ─────────────────────────────────────────────────────

    def save(self, checkpoint_dir: str) -> Path:
        """Write current state to ``<checkpoint_dir>/<ticket_id>.json``."""
        dirpath = Path(checkpoint_dir)
        dirpath.mkdir(parents=True, exist_ok=True)
        filepath = dirpath / f"{self.ticket_id}.json"
        tmp = filepath.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, default=str)
        # Atomic-ish rename (best-effort on Windows).
        tmp.replace(filepath)
        logger.debug("Checkpoint saved → %s", filepath)
        return filepath

    @classmethod
    def load(cls, ticket_id: str, checkpoint_dir: str) -> Checkpoint | None:
        """Load a checkpoint from disk, or return *None* if none exists."""
        filepath = Path(checkpoint_dir) / f"{ticket_id}.json"
        if not filepath.exists():
            return None
        with open(filepath, encoding="utf-8") as fh:
            data = json.load(fh)
        logger.info("Checkpoint loaded ← %s (stage=%s, iter=%s)",
                     filepath, data.get("current_stage"), data.get("iteration_count"))
        return cls(**data)

    @staticmethod
    def delete(ticket_id: str, checkpoint_dir: str) -> None:
        """Remove a checkpoint file after a pipeline run completes."""
        filepath = Path(checkpoint_dir) / f"{ticket_id}.json"
        if filepath.exists():
            os.remove(filepath)
            logger.info("Checkpoint deleted: %s", filepath)
