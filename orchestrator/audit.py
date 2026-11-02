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
    """Append-only JSONL audit log and markdown chat trace scoped to a single ticket."""

    def __init__(self, ticket_id: str, audit_dir: str) -> None:
        self._ticket_id = ticket_id
        self._dir = Path(audit_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._filepath = self._dir / f"{ticket_id}.jsonl"
        self._trace_filepath = self._dir / f"{ticket_id}_trace.md"
        self._interaction_count = 0

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

    def log_llm_interaction(
        self,
        stage: int,
        prompt: str | list[dict[str, Any]],
        response: str | dict[str, Any],
        model: str,
        latency_seconds: float = 0.0,
        usage: dict[str, int] | None = None,
        attached_files: list[str] | dict[str, str] | None = None,
        call_purpose: str = "",
    ) -> None:
        """
        Record a full LLM interaction into both the JSONL audit trail
        and a human-readable markdown chat trace file.
        """
        self._interaction_count += 1
        now_iso = datetime.now(timezone.utc).isoformat()

        if isinstance(prompt, list):
            prompt_str = json.dumps(prompt, indent=2, default=str)
        else:
            prompt_str = str(prompt)

        if isinstance(response, dict):
            response_content = response.get("content", "")
            if not response_content and "tool_calls" in response:
                response_str = json.dumps(response.get("tool_calls"), indent=2, default=str)
            else:
                response_str = str(response_content)
        else:
            response_str = str(response)

        file_list: list[str] = []
        if isinstance(attached_files, dict):
            file_list = list(attached_files.keys())
        elif isinstance(attached_files, list):
            file_list = [str(f) for f in attached_files]

        # 1. Append structured event to JSONL
        self.log(
            stage=stage,
            event_type="llm_interaction",
            payload={
                "interaction_index": self._interaction_count,
                "call_purpose": call_purpose or f"Stage {stage} LLM call",
                "model": model,
                "latency_seconds": round(latency_seconds, 2),
                "usage": usage or {},
                "attached_files": file_list,
                "prompt": prompt_str,
                "response": response_str,
            },
        )

        # 2. Append human-readable turn to Markdown trace
        self._append_to_trace(
            index=self._interaction_count,
            timestamp=now_iso,
            stage=stage,
            call_purpose=call_purpose or f"Stage {stage} LLM Call",
            model=model,
            latency_seconds=latency_seconds,
            usage=usage or {},
            file_list=file_list,
            prompt_str=prompt_str,
            response_str=response_str,
        )

    def _append_to_trace(
        self,
        index: int,
        timestamp: str,
        stage: int,
        call_purpose: str,
        model: str,
        latency_seconds: float,
        usage: dict[str, int],
        file_list: list[str],
        prompt_str: str,
        response_str: str,
    ) -> None:
        if not self._trace_filepath.exists():
            with open(self._trace_filepath, "w", encoding="utf-8") as fh:
                fh.write(f"# 📜 LLM Execution & Chat Trace — Ticket #{self._ticket_id}\n\n")
                fh.write(f"> **Audit Log Source:** `{self._filepath.name}`  \n")
                fh.write(f"> **Generated:** `{timestamp}`\n\n---\n\n")

        prompt_tok = usage.get("prompt_tokens", "N/A")
        comp_tok = usage.get("completion_tokens", "N/A")
        files_md = ", ".join(f"`{f}`" for f in file_list) if file_list else "*(None explicitly attached)*"

        entry = (
            f"## Turn {index}: Stage {stage} — {call_purpose}\n\n"
            f"- **Timestamp:** `{timestamp}`\n"
            f"- **Model:** `{model}`\n"
            f"- **Latency:** {latency_seconds:.1f}s | **Tokens:** Prompt: `{prompt_tok}`, Completion: `{comp_tok}`\n"
            f"- **Attached / Context Files:** {files_md}\n\n"
            f"### 📥 Prompt Sent to LLM\n\n"
            f"```text\n{prompt_str}\n```\n\n"
            f"### 📤 LLM Response Generated\n\n"
            f"```text\n{response_str}\n```\n\n"
            f"---\n\n"
        )
        with open(self._trace_filepath, "a", encoding="utf-8") as fh:
            fh.write(entry)

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

    @property
    def trace_filepath(self) -> Path:
        return self._trace_filepath
