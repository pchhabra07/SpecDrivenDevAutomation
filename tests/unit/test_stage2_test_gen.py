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

    def test_python_with_html_templates_detected_as_python_not_static(self):
        # A Django or Flask app with HTML templates should be detected as Python
        ctx = _make_context(["templates/index.html", "static/style.css", "app.py", "requirements.txt"])
        assert _detect_project_stack(ctx) == "python"

    def test_detects_java(self):
        ctx = _make_context(["pom.xml", "src/main/java/App.java"])
        assert _detect_project_stack(ctx) == "java"

    def test_detects_go(self):
        ctx = _make_context(["go.mod", "main.go"])
        assert _detect_project_stack(ctx) == "go"


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
        tests, ci = _generate_tests(mock_llm, ctx)

        assert "tests/background.test.js" in tests
        call_prompt = mock_llm.chat.call_args[0][0]
        assert "Static Web" in call_prompt
        assert "DO NOT generate tests for `package.json`" in call_prompt
        assert "DO NOT import React" in call_prompt

    def test_generates_workflow_alongside_tests(self):
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "content": (
                "// FILE: tests/test_app.py\ndef test_feature(): pass\n\n"
                "// FILE: .github/workflows/ci.yml\nname: CI\njobs: []\n"
            )
        }
        ctx = _make_context(
            all_paths=["app.py", "requirements.txt"],
            source_files={"app.py": "def run(): pass"},
        )
        tests, ci = _generate_tests(mock_llm, ctx)

        assert "tests/test_app.py" in tests
        assert ci is not None
        assert ci[0] == ".github/workflows/ci.yml"
        assert "name: CI" in ci[1]


class TestReviseTests:
    def test_revised_tests_drops_removed_files(self):
        mock_llm = MagicMock()
        mock_llm.chat.return_value = {
            "content": "// FILE: tests/background.test.js\nconst fs = require('fs');\n"
        }
        ctx = _make_context(["index.html", "style.css"])
        current = {
            "src/__tests__/App.test.tsx": "import React from 'react';",
            "tests/background.test.js": "// old test",
        }
        revised, ci = _revise_tests(
            llm=mock_llm,
            context=ctx,
            current_tests=current,
            reviewer_comments=["Remove App.test.tsx as this is not a React project"],
        )

        assert "tests/background.test.js" in revised
        assert "src/__tests__/App.test.tsx" not in revised
