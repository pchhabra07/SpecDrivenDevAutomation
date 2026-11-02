"""Unit tests for orchestrator.preflight_checker (Phase 3)."""
import pytest
from orchestrator.preflight_checker import (
    PreflightResult,
    build_repair_prompt,
    check_all,
)


class TestCheckPython:
    def test_valid_python(self):
        ok, results = check_all({"app.py": "x = 1\nprint(x)"})
        assert ok
        assert results[0].passed

    def test_syntax_error_python(self):
        ok, results = check_all({"bad.py": "def foo(:\n    pass"})
        assert not ok
        assert not results[0].passed
        assert "SyntaxError" in results[0].errors[0]


class TestCheckCSS:
    def test_valid_css(self):
        ok, results = check_all({"style.css": "body { color: red; }"})
        assert ok

    def test_unbalanced_css(self):
        ok, results = check_all({"style.css": "body { color: red;"})
        assert not ok
        assert "Unbalanced" in results[0].errors[0]


class TestCheckHTML:
    def test_valid_html(self):
        ok, results = check_all({"index.html": "<div><p>hello</p></div>"})
        assert ok

    def test_unclosed_html(self):
        ok, results = check_all({"index.html": "<div><p>hello</p>"})
        assert not ok

    def test_void_elements_ok(self):
        ok, results = check_all({"index.html": "<div><img src='x.png'><br></div>"})
        assert ok


class TestCheckJSON:
    def test_valid_json(self):
        ok, results = check_all({"data.json": '{"key": "value"}'})
        assert ok

    def test_invalid_json(self):
        ok, results = check_all({"data.json": '{key: value}'})
        assert not ok
        assert "JSONDecodeError" in results[0].errors[0]


class TestCheckJavaScript:
    def test_balanced_brackets(self):
        ok, results = check_all({"app.js": "function foo() { return 1; }"})
        assert ok

    def test_unbalanced_brackets(self):
        ok, results = check_all({"app.js": "function foo() { return 1;"})
        assert not ok


class TestCheckAll:
    def test_all_pass(self):
        files = {
            "a.py": "x = 1",
            "b.css": "body { color: red; }",
            "c.json": '{"ok": true}',
        }
        ok, results = check_all(files)
        assert ok
        assert all(r.passed for r in results)

    def test_one_fail(self):
        files = {
            "good.py": "x = 1",
            "bad.py": "def bad(:\n    pass",
        }
        ok, _ = check_all(files)
        assert not ok


class TestBuildRepairPrompt:
    def test_prompt_contains_errors(self):
        failed = [
            PreflightResult(path="bad.py", passed=False, language="python",
                            errors=["SyntaxError on line 3: invalid syntax"]),
        ]
        prompt = build_repair_prompt(failed)
        assert "bad.py" in prompt
        assert "SyntaxError" in prompt
        assert "SEARCH/REPLACE" in prompt

    def test_prompt_excludes_passing_files(self):
        results = [
            PreflightResult(path="good.py", passed=True, language="python"),
            PreflightResult(path="bad.py", passed=False, language="python",
                            errors=["some error"]),
        ]
        prompt = build_repair_prompt(results)
        assert "bad.py" in prompt
        assert "good.py" not in prompt
