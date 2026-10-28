"""
Unit tests covering Stage 3 fixes for problems S3-1 through S3-7:
- S3-4: Stale Stage-1 file content in prompt vs current_sources; blocked/locked path exclusion.
- S3-1: Patch failure re-prompting with current file content; all-or-nothing exhaustion guard.
- S3-3: Tiered source file inclusion; failing test paths extraction; full content on iter 1.
- S3-2: _fuzzy_replace ambiguity rejection when multiple normalized matches exist.
- S3-7: Preflight JS/TS template literal neutralization, real JS parser check, confidence tracking.
- S3-6: Reviewer feedback history tracking and prompt formatting.
- S3-5: Checkpoint self-contained directory and save/load without manual dir inference.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.checkpoint import Checkpoint
from orchestrator.diff_patcher import (
    SRBlock,
    _apply_block,
    _fuzzy_replace,
    apply_patches,
    apply_patches_to_sources,
)
from orchestrator.models import TicketData
from orchestrator.preflight_checker import (
    PreflightResult,
    _check_file,
    _check_js_ts,
    _strip_js_comments,
    build_repair_prompt,
    check_all,
)
from orchestrator.stages.stage1_context import TicketContext
from orchestrator.stages.stage3_code_gen import (
    _build_patch_failure_prompt,
    _extract_failing_file_paths,
    _format_reviewer_feedback,
    _generate_code,
)


def _make_ticket_context(
    source_files: dict[str, str] | None = None,
    test_files: dict[str, str] | None = None,
    llm_selected_paths: list[str] | None = None,
) -> TicketContext:
    ctx = TicketContext(
        ticket=TicketData(
            ticket_id="TEST-301",
            summary="Test stage 3 fixes",
            description="Fix rendering issues and improve performance.",
            acceptance_criteria="- Pass all tests\n- No syntax errors",
            labels=["SpecDrivenDev"],
        ),
        default_branch="main",
        source_files=source_files or {"src/app.ts": "// OLD CONTENT FROM STAGE 1"},
        test_files=test_files or {"tests/app.test.ts": "test('ok', () => {});"},
        all_file_paths=list((source_files or {"src/app.ts": ""}).keys()),
    )
    if llm_selected_paths is not None:
        ctx.llm_selected_paths = llm_selected_paths
    return ctx


# ── S3-4: Stale File Content & Blocked Paths ────────────────────────────


class TestS3_4_StaleContentAndBlockedPaths:
    def test_generate_code_uses_current_sources_not_context_source_files(self):
        context = _make_ticket_context(source_files={"src/app.ts": "// OLD STAGE 1 CONTENT"})
        current_sources = {"src/app.ts": "// FRESH CONTENT AFTER ITER 1"}

        captured_prompts = []
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = lambda prompt, **kwargs: (
            captured_prompts.append(prompt),
            {"content": "// FILE: src/app.ts\n<<<<<<< SEARCH\n// FRESH CONTENT AFTER ITER 1\n=======\n// UPDATED\n>>>>>>> REPLACE"},
        )[1]

        _generate_code(
            llm=mock_llm,
            context=context,
            current_sources=current_sources,
            last_test_output="",
            iteration=2,
            blocked_paths=set(),
        )

        assert len(captured_prompts) > 0
        prompt = captured_prompts[0]
        assert "// FRESH CONTENT AFTER ITER 1" in prompt
        assert "// OLD STAGE 1 CONTENT" not in prompt

    def test_generate_code_excludes_blocked_and_locked_paths_from_sources(self):
        context = _make_ticket_context(source_files={})
        current_sources = {
            "src/service.ts": "export class Service {}",
            "tests/locked.test.ts": "test('locked', () => {});",
            ".github/workflows/ci.yml": "name: CI",
        }
        blocked = {"tests/locked.test.ts", ".github/workflows/ci.yml"}

        captured_prompts = []
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = lambda prompt, **kwargs: (
            captured_prompts.append(prompt),
            {"content": "// FILE: src/service.ts\n<<<<<<< SEARCH\nexport class Service {}\n=======\nexport class Service { run() {} }\n>>>>>>> REPLACE"},
        )[1]

        _generate_code(
            llm=mock_llm,
            context=context,
            current_sources=current_sources,
            last_test_output="",
            iteration=1,
            blocked_paths=blocked,
        )

        prompt = captured_prompts[0]
        # src/service.ts must appear under Current Source Files
        assert "### `src/service.ts`" in prompt
        # Locked tests & CI workflow must NOT appear in Current Source Files
        source_section = prompt.split("## Current Source Files")[1].split("## Locked Test Files")[0]
        assert "tests/locked.test.ts" not in source_section
        assert ".github/workflows/ci.yml" not in source_section


# ── S3-1: Patch Failures & Immediate LLM Repair ─────────────────────────


class TestS3_1_PatchFailuresAndRepair:
    def test_build_patch_failure_prompt_includes_file_contents_and_diagnostics(self):
        patch_failures = ["FILE `src/calc.ts`: NOT FOUND: SEARCH block not found in file content."]
        failed_files = ["src/calc.ts"]
        current_sources = {"src/calc.ts": "export function add(a: number, b: number) { return a + b; }"}

        prompt = _build_patch_failure_prompt(patch_failures, failed_files, current_sources)

        assert "FILE `src/calc.ts`: NOT FOUND" in prompt
        assert "export function add(a: number, b: number) { return a + b; }" in prompt
        assert "Exact Current File Contents" in prompt
        assert "SEARCH/REPLACE" in prompt

    def test_generate_code_reprompts_with_current_file_content_on_patch_failure(self):
        context = _make_ticket_context()
        current_sources = {"src/app.ts": "const state = { count: 0 };"}

        captured_prompts = []

        def mock_chat(prompt, **kwargs):
            captured_prompts.append(prompt)
            if len(captured_prompts) == 1:
                # First attempt: wrong search text that won't match
                return {
                    "content": (
                        "### 1. ARCHITECTURAL PLAN\nPlan\n"
                        "### 2. CODE EDITS\n"
                        "// FILE: src/app.ts\n"
                        "<<<<<<< SEARCH\n"
                        "const state = { count: 999 };\n"
                        "=======\n"
                        "const state = { count: 1 };\n"
                        ">>>>>>> REPLACE"
                    )
                }
            # Second attempt: LLM corrects its SEARCH block using the provided current file content
            return {
                "content": (
                    "### 1. ARCHITECTURAL PLAN\nPlan\n"
                    "### 2. CODE EDITS\n"
                    "// FILE: src/app.ts\n"
                    "<<<<<<< SEARCH\n"
                    "const state = { count: 0 };\n"
                    "=======\n"
                    "const state = { count: 1 };\n"
                    ">>>>>>> REPLACE"
                )
            }

        mock_llm = MagicMock()
        mock_llm.chat.side_effect = mock_chat

        result = _generate_code(
            llm=mock_llm,
            context=context,
            current_sources=current_sources,
            last_test_output="",
            iteration=1,
        )

        assert len(captured_prompts) == 2
        # Second prompt must contain patch failure details and current file content
        assert "Exact Current File Contents" in captured_prompts[1]
        assert "const state = { count: 0 };" in captured_prompts[1]
        assert "src/app.ts" in result
        assert "count: 1" in result["src/app.ts"]

    def test_generate_code_returns_empty_when_patch_failures_exhaust_attempts(self):
        context = _make_ticket_context()
        current_sources = {"src/app.ts": "const x = 1;"}

        # Model repeatedly returns invalid SEARCH block
        bad_response = {
            "content": (
                "// FILE: src/app.ts\n"
                "<<<<<<< SEARCH\n"
                "nonexistent line;\n"
                "=======\n"
                "replacement;\n"
                ">>>>>>> REPLACE"
            )
        }
        mock_llm = MagicMock()
        mock_llm.chat.return_value = bad_response

        result = _generate_code(
            llm=mock_llm,
            context=context,
            current_sources=current_sources,
            last_test_output="",
            iteration=1,
        )

        # Must return {} to prevent partial or broken file commits
        assert result == {}


# ── S3-2: Fuzzy Replace Ambiguity ───────────────────────────────────────


class TestS3_2_FuzzyReplaceAmbiguity:
    def test_fuzzy_replace_unique_match_succeeds(self):
        content = "function test() {\n    return 42;\n}"
        search = "return 42;"
        replace = "return 100;"
        out, ok = _fuzzy_replace(content, search, replace)
        assert ok
        assert "return 100;" in out

    def test_fuzzy_replace_multiple_matches_rejected(self):
        # File has repeated identical normalized lines (e.g. repeated braces or returns)
        content = "if (a) {\n    return null;\n}\nif (b) {\n    return null;\n}"
        search = "return null;"
        replace = "return true;"
        out, ok = _fuzzy_replace(content, search, replace)
        # Ambiguous match must be rejected to prevent patching the wrong occurrence
        assert not ok
        assert out == content

    def test_apply_block_reports_fuzzy_ambiguous(self):
        content = "line 1\n    return  null;\nline 3\n      return   null;\nline 5"
        blk = SRBlock(search="return null;", replace="return 1;")
        patched, err = _apply_block(content, blk)
        assert err is not None
        assert "FUZZY_AMBIGUOUS" in err
        assert "2 locations" in err

    def test_apply_block_reports_exact_ambiguous(self):
        content = "dup\ndup"
        blk = SRBlock(search="dup", replace="unique")
        patched, err = _apply_block(content, blk)
        assert err is not None
        assert "AMBIGUOUS: 2 exact occurrences found" in err


# ── S3-3: Tiered File Inclusion & Failure-Driven Slicing ─────────────────


class TestS3_3_TieredFileInclusion:
    def test_extract_failing_file_paths_from_ci_output(self):
        current_sources = {
            "src/components/Header.tsx": "header",
            "src/utils/calc.ts": "calc",
            "src/service.ts": "service",
        }
        ci_output = (
            "FAIL src/__tests__/calc.test.ts\n"
            "  ● Test addition\n"
            "    expect(received).toBe(expected)\n"
            "    at Object.<anonymous> (src/utils/calc.ts:24:12)\n"
        )
        failing = _extract_failing_file_paths(ci_output, current_sources)
        assert "src/utils/calc.ts" in failing
        assert "src/components/Header.tsx" not in failing

    def test_tiered_prompt_full_content_for_failing_and_preview_for_others(self):
        long_unrelated = "\n".join(f"const line{i} = {i};" for i in range(100))
        failing_content = "export function solve() { return false; }"
        current_sources = {
            "src/failing.ts": failing_content,
            "src/unrelated.ts": long_unrelated,
        }
        context = _make_ticket_context(source_files=current_sources)
        ci_output = "Error in src/failing.ts at line 5"

        captured = []
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = lambda prompt, **kwargs: (
            captured.append(prompt),
            {"content": "// FILE: src/failing.ts\n<<<<<<< SEARCH\nreturn false;\n=======\nreturn true;\n>>>>>>> REPLACE"},
        )[1]

        _generate_code(
            llm=mock_llm,
            context=context,
            current_sources=current_sources,
            last_test_output=ci_output,
            iteration=2,
        )

        prompt = captured[0]
        # Failing file gets full content label and complete text
        assert "src/failing.ts" in prompt
        assert "FAILING TEST REFERENCES THIS FILE" in prompt
        assert failing_content in prompt

        # Unrelated file > 60 lines gets preview label and truncated note
        assert "src/unrelated.ts" in prompt
        assert "preview" in prompt
        assert "more lines omitted" in prompt

    def test_iteration_1_provides_full_content_for_priority_files(self):
        # On iteration 1, tests haven't run yet; don't truncate priority files
        content = "\n".join(f"const x{i} = {i};" for i in range(40))
        current_sources = {"src/primary.ts": content}
        context = _make_ticket_context(source_files=current_sources)

        captured = []
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = lambda prompt, **kwargs: (
            captured.append(prompt),
            {"content": "// FILE: src/primary.ts\nconst full = true;"},
        )[1]

        _generate_code(
            llm=mock_llm,
            context=context,
            current_sources=current_sources,
            last_test_output="",
            iteration=1,
        )

        prompt = captured[0]
        assert content in prompt
        assert "preview — request full content" not in prompt


# ── S3-7: Preflight Syntax Checking & Template Literals ──────────────────


class TestS3_7_PreflightParserAndConfidence:
    def test_strip_js_comments_neutralizes_template_literals(self):
        code = "const msg = `hello {user} {extra`;"
        cleaned = _strip_js_comments(code)
        # Template literal content replaced with ``
        assert "{user}" not in cleaned
        assert "{extra" not in cleaned

    def test_preflight_valid_js_with_template_literal_braces_passes(self):
        code = 'const msg = `Value is: ${item.id} with brace { in string`;'
        result = _check_js_ts("app.js", code)
        assert result.passed

    def test_preflight_confidence_level(self):
        # Real node check on valid pure JS produces confidence="high"
        res_js = _check_js_ts("app.js", "const a = 1; function foo() { return a; }")
        assert res_js.passed
        assert res_js.confidence == "high"

        # TS heuristic check produces confidence="low"
        res_ts = _check_js_ts("app.ts", "interface User { id: number; }")
        assert res_ts.confidence == "low"

    def test_build_repair_prompt_includes_confidence_note(self):
        low_conf = [
            PreflightResult(path="app.ts", passed=False, language="ts", errors=["Unbalanced: 2x '{' vs 1x '}'"], confidence="low")
        ]
        prompt = build_repair_prompt(low_conf)
        assert "(heuristic check)" in prompt
        assert "app.ts" in prompt


# ── S3-6: Reviewer Feedback History ─────────────────────────────────────


class TestS3_6_ReviewerFeedbackHistory:
    def test_format_reviewer_feedback_single_cycle(self):
        feedback = ["Fix CSS button padding", "Add error toast"]
        result = _format_reviewer_feedback(feedback, history=[])
        assert "Fix CSS button padding" in result
        assert "Add error toast" in result
        assert "previous review cycle" not in result

    def test_format_reviewer_feedback_with_history(self):
        cycle1 = ["Ensure mobile responsiveness"]
        cycle2 = ["Fix button padding"]
        history = [cycle1, cycle2]

        result = _format_reviewer_feedback(cycle2, history=history)
        assert "Fix button padding" in result
        assert "1 previous review cycle(s) also requested changes" in result
        assert "Ensure mobile responsiveness" in result

    def test_checkpoint_serializes_reviewer_feedback_history(self, tmp_path):
        cp = Checkpoint(ticket_id="TEST-301", checkpoint_dir=str(tmp_path))
        cp.reviewer_feedback = ["Fix styling"]
        cp.reviewer_feedback_history = [["Add tests"], ["Fix styling"]]
        cp.save()

        loaded = Checkpoint.load("TEST-301", str(tmp_path))
        assert loaded is not None
        assert loaded.reviewer_feedback == ["Fix styling"]
        assert loaded.reviewer_feedback_history == [["Add tests"], ["Fix styling"]]


# ── S3-5: Checkpoint Self-Contained Directory ───────────────────────────


class TestS3_5_CheckpointDirectory:
    def test_save_uses_checkpoint_dir_without_argument(self, tmp_path):
        cp = Checkpoint(ticket_id="TEST-302", checkpoint_dir=str(tmp_path))
        saved_path = cp.save()
        assert saved_path.exists()
        assert saved_path.parent == tmp_path

    def test_load_assigns_checkpoint_dir(self, tmp_path):
        cp = Checkpoint(ticket_id="TEST-303", checkpoint_dir=str(tmp_path))
        cp.save()

        loaded = Checkpoint.load("TEST-303", str(tmp_path))
        assert loaded is not None
        assert loaded.checkpoint_dir == str(tmp_path)
        # Subsequent save with no arguments should work seamlessly
        saved_again = loaded.save()
        assert saved_again.exists()
