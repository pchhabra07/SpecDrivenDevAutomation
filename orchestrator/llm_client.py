"""
LLM client — thin wrapper around the OpenAI SDK pointed at NVIDIA NIM.

Responsibilities:
- Maintains full in-memory conversation history for the current ticket
  (system prompt + user/assistant/tool messages).  History is never
  evicted during rate-limit pauses (FR-29).
- Routes every call through ``RateLimiter`` (FR-28).
- Supports tool-call round-trips.
- Falls back to ``NIM_FALLBACK_MODEL`` after 3 consecutive primary
  failures.

All retry/backoff for transient server errors is handled here so that
stage modules can call ``chat()`` without worrying about HTTP mechanics.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from openai import OpenAI, APIError, APITimeoutError, RateLimitError

from orchestrator.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0  # seconds


class LLMClient:
    """Stateful LLM chat client backed by NVIDIA NIM."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        fallback_model: str,
        rate_limiter: RateLimiter,
        system_prompt: str = "",
    ) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._fallback_model = fallback_model
        self._rate_limiter = rate_limiter

        self._history: list[dict[str, Any]] = []
        if system_prompt:
            self._history.append({"role": "system", "content": system_prompt})

        self._consecutive_failures = 0

    # ── Public API ──────────────────────────────────────────────────────

    def chat(
        self,
        user_message: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """
        Send a user message and return the assistant's response message dict.

        Automatically appends both the user message and the assistant
        response to the conversation history.
        """
        self._history.append({"role": "user", "content": user_message})
        response_msg = self._call_with_retry(tools=tools, temperature=temperature)
        self._history.append(response_msg)
        return response_msg

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """Append a tool-result message so the next ``chat`` round can continue."""
        self._history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    def continue_after_tool_calls(
        self,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """
        Continue the conversation after tool results have been added.

        Returns the assistant's next response (which may itself contain
        more tool calls).
        """
        response_msg = self._call_with_retry(tools=tools, temperature=temperature)
        self._history.append(response_msg)
        return response_msg

    # ── History management ──────────────────────────────────────────────

    @property
    def history(self) -> list[dict[str, Any]]:
        """Return the full conversation history (read-only view)."""
        return list(self._history)

    def set_history(self, history: list[dict[str, Any]]) -> None:
        """Replace the conversation history (used when restoring from checkpoint)."""
        self._history = list(history)

    def clear_history(self, keep_system: bool = True) -> None:
        """Reset conversation history, optionally keeping the system prompt."""
        if keep_system and self._history and self._history[0]["role"] == "system":
            self._history = [self._history[0]]
        else:
            self._history = []

    # ── Internals ───────────────────────────────────────────────────────

    def _active_model(self) -> str:
        if self._consecutive_failures >= _MAX_RETRIES:
            logger.warning(
                "Primary model failed %d times; switching to fallback: %s",
                self._consecutive_failures,
                self._fallback_model,
            )
            return self._fallback_model
        return self._model

    def _call_with_retry(
        self,
        tools: list[dict[str, Any]] | None,
        temperature: float,
    ) -> dict[str, Any]:
        """Attempt the chat completion with exponential backoff."""
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            self._rate_limiter.acquire()
            model = self._active_model()
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": self._history,
                    "temperature": temperature,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                response = self._client.chat.completions.create(**kwargs)
                msg = response.choices[0].message.model_dump()
                self._consecutive_failures = 0
                return msg

            except (APIError, APITimeoutError, RateLimitError) as exc:
                last_exc = exc
                self._consecutive_failures += 1
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    "NIM call failed (attempt %d/%d, model=%s): %s — retrying in %.1fs",
                    attempt,
                    _MAX_RETRIES,
                    model,
                    exc,
                    wait,
                )
                time.sleep(wait)

        raise RuntimeError(
            f"NIM API call failed after {_MAX_RETRIES} retries"
        ) from last_exc
