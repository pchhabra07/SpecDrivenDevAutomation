"""
Custom orchestrator exceptions.
"""

from __future__ import annotations


class GatePendingError(Exception):
    """
    Raised when a human approval gate is pending in non-blocking mode.
    Signals the orchestrator runner to suspend pipeline execution cleanly
    and park the ticket until a review/webhook event re-triggers it.
    """

    def __init__(self, gate_name: str, mr_iid: int):
        self.gate_name = gate_name
        self.mr_iid = mr_iid
        super().__init__(f"Gate '{gate_name}' pending on PR/MR #{mr_iid}")
