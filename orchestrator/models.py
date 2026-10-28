"""
Shared data models for the SpecDrivenDev pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TicketData:
    """Structured representation of a GitHub Issue (or ticket) for the pipeline."""

    ticket_id: str
    summary: str
    description: str
    acceptance_criteria: str
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    assignee: str | None = None
    reporter: str | None = None
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class MergeRequestInfo:
    """Essential metadata about a created or fetched Pull Request / Merge Request."""

    iid: int
    web_url: str
    state: str = "opened"
    draft: bool = False
    number: int = 0

    def __post_init__(self) -> None:
        if not self.number and self.iid:
            self.number = self.iid
        elif not self.iid and self.number:
            self.iid = self.number


@dataclass
class PipelineResult:
    """Terminal or in-progress result of a CI pipeline / workflow run."""

    pipeline_id: int
    status: str
    web_url: str = ""
    jobs: list[dict[str, Any]] = field(default_factory=list)
