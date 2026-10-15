"""Unit tests for orchestrator.diff_patcher (Phase 1)."""
import pytest
from orchestrator.diff_patcher import (
    PatchResult,
    SRBlock,
    apply_patches,
    apply_patches_to_sources,
    parse_llm_response,
    _strip_fences,
    _fuzzy_replace,
)

SM = "<" * 7 + " SEARCH"
SEP = "=" * 7
RM = ">" * 7 + " REPLACE"


def make_llm_output(path: str, search: str, replace: str) -> str:
    return (
        f"// FILE: {path}\n"
        f"{SM}\n"
        f"{search}\n"
        f"{SEP}\n"
        f"{replace}\n"
        f"{RM}\n"
    )


class TestParseEmptyResponse:
    def test_empty_string(self):
        assert parse_llm_response("") == {}

    def test_no_file_marker(self):
        assert parse_llm_response("some random text without FILE markers") == {}


class TestParseSearchReplaceBlocks:
    def test_single_block(self):
        out = make_llm_output("src/app.py", "old line", "new line")
        result = parse_llm_response(out)
        assert "src/app.py" in result
        blocks = result["src/app.py"]
        assert len(blocks) == 1
        assert blocks[0].search == "old line"
        assert blocks[0].replace == "new line"

    def test_multiple_files(self):
        out = (
            make_llm_output("a.py", "x = 1", "x = 2")
            + make_llm_output("b.css", ".old {}", ".new {}")
        )
        result = parse_llm_response(out)
        assert "a.py" in result
        assert "b.css" in result

    def test_new_file_no_blocks(self):
        out = "// FILE: new_file.js\nconsole.log('hello');"
        result = parse_llm_response(out)
        assert "new_file.js" in result
        assert isinstance(result["new_file.js"], str)
        assert "console.log" in result["new_file.js"]


class TestApplyPatches:
    def test_exact_match(self):
        original = "line1\nold_code\nline3"
        blk = SRBlock(search="old_code", replace="new_code")
        pr = apply_patches("test.py", original, [blk])
        assert pr.success
        assert "new_code" in pr.patched_content
        assert "old_code" not in pr.patched_content
        assert pr.blocks_applied == 1

    def test_not_found(self):
        original = "line1\nline2\nline3"
        blk = SRBlock(search="nonexistent_text", replace="replacement")
        pr = apply_patches("test.py", original, [blk])
        assert not pr.success
        assert pr.blocks_failed == 1
        assert "NOT FOUND" in pr.errors[0]

    def test_ambiguous_match(self):
        original = "dup\ndup\nother"
        blk = SRBlock(search="dup", replace="unique")
        pr = apply_patches("test.py", original, [blk])
        assert not pr.success
        assert "AMBIGUOUS" in pr.errors[0]

    def test_new_file_write(self):
        pr = apply_patches("new.py", None, "brand new content")
        assert pr.success
        assert pr.is_new_file
        assert pr.patched_content == "brand new content"

    def test_multiple_blocks(self):
        original = "a = 1\nb = 2\nc = 3"
        blocks = [
            SRBlock(search="a = 1", replace="a = 10"),
            SRBlock(search="c = 3", replace="c = 30"),
        ]
        pr = apply_patches("test.py", original, blocks)
        assert pr.success
        assert pr.blocks_applied == 2
        assert "a = 10" in pr.patched_content
        assert "c = 30" in pr.patched_content


class TestFuzzyReplace:
    def test_whitespace_normalised(self):
        content = "  x  =  1"
        search = "x = 1"
        replace = "x = 99"
        out, ok = _fuzzy_replace(content, search, replace)
        assert ok
        assert "99" in out

    def test_no_match(self):
        _, ok = _fuzzy_replace("abc", "xyz", "zzz")
        assert not ok


class TestStripFences:
    def test_removes_fences(self):
        assert _strip_fences("```\ncode\n```") == "code"

    def test_no_fences(self):
        assert _strip_fences("plain text") == "plain text"


class TestApplyPatchesToSources:
    def test_updates_sources(self):
        sources = {"app.py": "x = 1\ny = 2"}
        blk = SRBlock(search="x = 1", replace="x = 99")
        parsed = {"app.py": [blk]}
        updated, results, all_succeeded = apply_patches_to_sources(parsed, sources)
        assert "app.py" in updated
        assert "x = 99" in updated["app.py"]
        assert results[0].success
        assert all_succeeded is True

    def test_new_file_added(self):
        sources = {}
        parsed = {"new.py": "new content"}
        updated, results, all_succeeded = apply_patches_to_sources(parsed, sources)
        assert "new.py" in updated
        assert results[0].is_new_file
        assert all_succeeded is True
