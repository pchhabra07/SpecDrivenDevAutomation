"""
Unit tests for orchestrator.stages.stage2_test_gen.
"""

from unittest.mock import MagicMock

from orchestrator.jira_client import TicketData
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.stages.stage2_test_gen import _detect_project_stack, _generate_tests, _revise_tests


def _make_context(
    all_paths: list[str],
    source_files: dict[str, str] | None = None,
    test_files: dict[str, str] | None = None,
) -> TicketContext:
    ticket = TicketData(
        ticket_id="TEST-1",
        summary="Change background to orange",
        description="Update background color to orange",
        acceptance_criteria="Background color must be orange",
        labels=["SpecDrivenDev"],
    )
    return TicketContext(
        ticket=ticket,
        default_branch="master",
        source_files=source_files or {},
        test_files=test_files or {},
        all_file_paths=all_paths,
    )


class TestDetectProjectStack:
    def test_detects_static_web(self):
        ctx = _make_context(["index.html", "style.css", "assets/logo.png"])
        assert _detect_project_stack(ctx) == "static_web"

    def test_detects_react(self):
        ctx = _make_context(["package.json", "src/App.tsx", "src/index.tsx"])
        assert _detect_project_stack(ctx) == "react"

    def test_detects_node(self):
        ctx = _make_context(["package.json", "index.js"])
        assert _detect_project_stack(ctx) == "node"

    def test_detects_python(self):
        ctx = _make_context(["requirements.txt", "main.py"])
        assert _detect_project_stack(ctx) == "python"


class TestGenerateTestsPrompt:
    def test_static_web_prompt_contains_prohibitions(self):
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "content": "// FILE: tests/background.test.js\nconst fs = require('fs');\n"
        }
        ctx = _make_context(
            all_paths=["index.html", "style.css"],
            source_files={"style.css": "body { background-color: white; }"},
        )
        tests = _generate_tests(mock_llm, ctx)

        assert "tests/background.test.js" in tests
        call_prompt = mock_llm.chat.call_args[0][0]
        assert "Static Web" in call_prompt
        assert "DO NOT generate tests for `package.json`" in call_prompt
        assert "DO NOT import React" in call_prompt


class TestReviseTests:
    def test_revised_tests_drops_removed_files(self):
        mock_llm = MagicMock()
        # Reviewer asked to remove App.test.tsx, so LLM only returns the remaining test
        mock_llm.chat.return_value = {
            "content": "// FILE: tests/background.test.js\nconst fs = require('fs');\n"
        }
        ctx = _make_context(["index.html", "style.css"])
        current = {
            "src/__tests__/App.test.tsx": "import React from 'react';",
            "tests/background.test.js": "// old test",
        }
        revised = _revise_tests(
            llm=mock_llm,
            context=ctx,
            current_tests=current,
            reviewer_comments=["Remove App.test.tsx as this is not a React project"],
        )

        assert "tests/background.test.js" in revised
        assert "src/__tests__/App.test.tsx" not in revised
