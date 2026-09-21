"""
Append-only JSONL audit logger.

Every significant pipeline event is recorded as a single JSON line in
``<audit_dir>/<ticket_id>.jsonl``.  Each entry is timestamped in UTC
and includes the pipeline stage, event type, and a free-form payload.

Implements FR-25.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLogger:
    """Append-only JSONL audit log scoped to a single ticket."""

    def __init__(self, ticket_id: str, audit_dir: str) -> None:
        self._ticket_id = ticket_id
        self._dir = Path(audit_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._filepath = self._dir / f"{ticket_id}.jsonl"

    def log(
        self,
        stage: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append a single audit entry."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticket_id": self._ticket_id,
            "stage": stage,
            "event_type": event_type,
            "payload": payload or {},
        }
        line = json.dumps(entry, default=str)
        with open(self._filepath, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.debug("Audit [%s] stage=%d event=%s", self._ticket_id, stage, event_type)

    def read_all(self) -> list[dict[str, Any]]:
        """Read back all entries (primarily for tests and debugging)."""
        if not self._filepath.exists():
            return []
        entries: list[dict[str, Any]] = []
        with open(self._filepath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    @property
    def filepath(self) -> Path:
        return self._filepath
