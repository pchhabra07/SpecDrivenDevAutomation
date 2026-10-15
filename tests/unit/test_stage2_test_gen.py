"""
Unit tests for orchestrator.stages.stage2_test_gen.
"""

from unittest.mock import MagicMock

from orchestrator.jira_client import TicketData
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.stages.stage2_test_gen import (
    _check_vacuous_assertions,
    _detect_project_stack,
    _generate_tests,
    _revise_tests,
    _validate_test_imports,
)


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
        # N2 fix: _revise_tests now calls chat_once() (stateless), NOT chat().
        mock_llm.chat_once.return_value = {
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

    def test_revise_uses_chat_once_not_chat(self):
        """N2 fix: revision must use stateless chat_once so history doesn't balloon."""
        mock_llm = MagicMock()
        mock_llm.chat_once.return_value = {
            "content": "// FILE: tests/test_ui.py\ndef test_dummy(): pass\n"
        }
        ctx = _make_context(["index.html", "style.css"])
        _revise_tests(
            llm=mock_llm,
            context=ctx,
            current_tests={"tests/test_ui.py": "old content"},
            reviewer_comments=["Update assertions"],
        )
        # chat_once must be called; plain chat must NOT be called
        mock_llm.chat_once.assert_called_once()
        mock_llm.chat.assert_not_called()

    def test_ci_failure_output_appears_in_revision_prompt(self):
        """N5 fix: CI failure output must be injected into the revision prompt."""
        mock_llm = MagicMock()
        mock_llm.chat_once.return_value = {
            "content": "// FILE: tests/test_ui.py\ndef test_something(): assert True\n"
        }
        ctx = _make_context(
            all_paths=["index.html", "style.css"],
            source_files={"style.css": "body { background: white; }"},
        )
        _revise_tests(
            llm=mock_llm,
            context=ctx,
            current_tests={"tests/test_ui.py": "old"},
            reviewer_comments=["Fix assertions"],
            ci_failure_output="AssertionError: expected dark background\nFAILED tests/test_ui.py::test_bg",
        )
        prompt_sent = mock_llm.chat_once.call_args[0][0]
        assert "CI Test Failure Output" in prompt_sent
        assert "AssertionError: expected dark background" in prompt_sent


# ── N3 Fix: Unconditional assertion static checker ──────────────────────


class TestCheckVacuousAssertions:
    """N3 fix: _check_vacuous_assertions detects tests where all asserts are
    inside if-guards (the silent-passing bug found in Issue #10)."""

    _GUARDED_ONLY = """\
def test_dark_background():
    style = parse_css()
    if 'background-color' in style:
        assert style['background-color'] == '#1a1a1a'
"""

    _UNCONDITIONAL_PRESENT = """\
def test_dark_background():
    style = parse_css()
    assert 'background-color' in style, 'must be set'
    assert style['background-color'] == '#1a1a1a'
"""

    _MIX_UNCONDITIONAL_FIRST = """\
def test_dark_background():
    style = parse_css()
    assert 'background-color' in style
    if 'color' in style:
        assert style['color'] == '#fff'
"""

    def test_flags_guarded_only_assertions(self):
        warnings: list[str] = []
        _check_vacuous_assertions("tests/test_ui.py", self._GUARDED_ONLY, warnings)
        assert len(warnings) == 1
        assert "test_dark_background" in warnings[0]
        assert "unconditional" in warnings[0]

    def test_passes_unconditional_assertions(self):
        warnings: list[str] = []
        _check_vacuous_assertions("tests/test_ui.py", self._UNCONDITIONAL_PRESENT, warnings)
        assert warnings == []

    def test_passes_mixed_with_unconditional_first(self):
        """If there's at least one unconditional assert, the function is fine."""
        warnings: list[str] = []
        _check_vacuous_assertions("tests/test_ui.py", self._MIX_UNCONDITIONAL_FIRST, warnings)
        assert warnings == []

    def test_skips_non_python_files(self):
        """Non-.py files must be skipped (no crash, no spurious warnings)."""
        warnings: list[str] = []
        _check_vacuous_assertions("tests/test_ui.js", self._GUARDED_ONLY, warnings)
        assert warnings == []

    def test_validate_test_imports_calls_vacuous_check(self):
        """_validate_test_imports must surface N3 warnings via _check_vacuous_assertions."""
        ctx = _make_context(
            all_paths=["index.html", "style.css"],
            source_files={"style.css": "body { background: white; }"},
        )
        # A test file where all asserts are if-guarded
        test_files = {"tests/test_ui.py": self._GUARDED_ONLY}
        warnings = _validate_test_imports(test_files, ctx)
        # Should contain at least one N3 warning
        n3_warnings = [w for w in warnings if "unconditional" in w]
        assert len(n3_warnings) >= 1
