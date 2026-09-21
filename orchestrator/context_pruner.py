"""
Context Window Pruner & Transcript Summarizer (Phase 5).

Addresses PRD Section 9 Risk:
  "Long-running tickets with many iterations approach the model's context window
   limit (since the full transcript is re-sent on every call)... plan for
   pruning/summarizing older tool outputs if a ticket's transcript grows very large."

Condenses intermediate iterations while preserving:
  1. System prompt
  2. Initial ticket specification and context
  3. The most recent 2-4 turns (latest code diffs, current Jest failure trace,
     and active reviewer comments)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """
    Heuristic token estimation (~4 characters per token for English/code).
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content") or ""
        total_chars += len(content)
        # Account for tool calls if any
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            total_chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return max(1, total_chars // 4)


def prune_transcript(
    history: list[dict[str, Any]],
    max_tokens: int = 16000,
    keep_last_turns: int = 4,
) -> list[dict[str, Any]]:
    """
    Prune older turns from conversation history if total tokens exceed max_tokens.

    Parameters
    ----------
    history:
        List of message dicts (OpenAI/NIM chat format).
    max_tokens:
        Token budget before pruning triggers.
    keep_last_turns:
        Number of trailing messages to preserve untouched.

    Returns
    -------
    list[dict[str, Any]]
        Pruned or original conversation history.
    """
    if len(history) <= (keep_last_turns + 2):
        return history

    tokens = estimate_tokens(history)
    if tokens <= max_tokens:
        return history

    logger.info(
        "Transcript has ~%d tokens (threshold: %d). Condensing older iterations.",
        tokens,
        max_tokens,
    )

    # 1. Separate system prompt and initial spec
    has_system = history[0].get("role") == "system"
    head_count = 2 if has_system else 1
    head_messages = history[:head_count]
    tail_messages = history[-keep_last_turns:]
    middle_messages = history[head_count:-keep_last_turns]

    # 2. Condense middle messages
    condensed_middle = []
    for msg in middle_messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""

        # If it's a large assistant code block or large test log, trim it
        if len(content) > 300:
            condensed_content = (
                f"[Condensed {role} turn ({len(content)} chars)]: "
                f"{content[:150]}... [truncated] ...{content[-100:]}"
            )
            new_msg = dict(msg)
            new_msg["content"] = condensed_content
            condensed_middle.append(new_msg)
        else:
            condensed_middle.append(msg)

    pruned = head_messages + condensed_middle + tail_messages
    new_tokens = estimate_tokens(pruned)
    logger.info("Transcript pruned from ~%d to ~%d tokens.", tokens, new_tokens)
    return pruned
