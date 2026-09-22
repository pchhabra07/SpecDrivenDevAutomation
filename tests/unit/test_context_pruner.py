"""
Unit tests for orchestrator.context_pruner (Phase 5).
"""

from orchestrator.context_pruner import estimate_tokens, prune_transcript


class TestContextPruner:
    def test_estimate_tokens(self):
        msgs = [{"role": "user", "content": "hello world! 1234"}]  # 18 chars -> ~4 tokens
        tokens = estimate_tokens(msgs)
        assert tokens == 4

    def test_estimate_tokens_with_none_tool_calls(self):
        msgs = [{"role": "assistant", "content": "hello", "tool_calls": None}]
        tokens = estimate_tokens(msgs)
        assert tokens == 1

    def test_prune_transcript_small_unchanged(self):
        history = [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "Fix bug PROJ-1"},
            {"role": "assistant", "content": "Here is diff 1"},
            {"role": "user", "content": "Jest result 1"},
        ]
        pruned = prune_transcript(history, max_tokens=1000)
        assert len(pruned) == len(history)
        assert pruned == history

    def test_prune_transcript_large_condenses_middle(self):
        # Build large transcript
        history = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Ticket spec PROJ-1"},
            # Turn 2: large middle assistant
            {"role": "assistant", "content": "A" * 1000},
            # Turn 3: large middle user (jest log)
            {"role": "user", "content": "B" * 2000},
            # Turn 4: large middle assistant
            {"role": "assistant", "content": "C" * 1500},
            # Tail turns
            {"role": "user", "content": "Latest jest failure"},
            {"role": "assistant", "content": "Latest diff"},
        ]

        # Trigger pruning by setting low max_tokens
        pruned = prune_transcript(history, max_tokens=100, keep_last_turns=2)

        assert len(pruned) == len(history)
        # Head preserved
        assert pruned[0]["content"] == "System prompt"
        assert pruned[1]["content"] == "Ticket spec PROJ-1"
        # Middle condensed
        assert "[Condensed assistant turn" in pruned[2]["content"]
        assert "[Condensed user turn" in pruned[3]["content"]
        assert "[Condensed assistant turn" in pruned[4]["content"]
        # Tail preserved
        assert pruned[-2]["content"] == "Latest jest failure"
        assert pruned[-1]["content"] == "Latest diff"

    def test_llm_client_context_pruning_integration(self, mocker):
        from unittest.mock import MagicMock
        from orchestrator.llm_client import LLMClient
        from orchestrator.rate_limiter import RateLimiter

        mock_limiter = MagicMock(spec=RateLimiter)
        llm = LLMClient(
            api_key="fake-key",
            base_url="http://fake",
            model="meta/llama-3.1-70b-instruct",
            fallback_model="meta/llama-3.1-8b-instruct",
            rate_limiter=mock_limiter,
            system_prompt="System Prompt",
            enable_context_pruning=True,
            max_context_tokens=50,
        )

        # Seed large history
        llm.append_user_message("Spec " + "X" * 200)
        llm.set_history([
            {"role": "system", "content": "System Prompt"},
            {"role": "user", "content": "Spec " + "X" * 100},
            {"role": "assistant", "content": "Old code " + "Y" * 500},
            {"role": "user", "content": "Old jest " + "Z" * 500},
            {"role": "assistant", "content": "Middle response"},
            {"role": "user", "content": "Middle error"},
            {"role": "assistant", "content": "Recent response"},
            {"role": "user", "content": "Recent error"},
        ])

        # Mock chat completion response
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(model_dump=lambda: {"role": "assistant", "content": "Fixed code"}))
        ]
        mocker.patch.object(llm._client.chat.completions, "create", return_value=mock_response)

        llm.continue_after_tool_calls()

        # History should have been pruned in middle turns
        history = llm.history
        assert "[Condensed assistant turn" in history[2]["content"]
        assert "[Condensed user turn" in history[3]["content"]
        assert history[-1]["content"] == "Fixed code"
