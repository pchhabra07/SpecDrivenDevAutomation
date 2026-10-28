"""
Unit and integration tests for all Stage 1 fixes (S1-1 through S1-6).

Covers:
  S1-1  - _build_snippet_index builds content-anchored previews and populates cache.
  S1-2  - _rank_relevant_files uses symbol-scored padding (not alphabetical) and
           emits a low-confidence audit log entry when the LLM returns too few files.
  S1-3  - search_repository_for_symbols uses the gitlab client in CI mode (no local
           clone) instead of silently returning an empty list.
  S1-4  - run() fetches at most max_context_test_files test files, sorted by symbol
           relevance.
  S1-5  - TicketContext.llm_selected_paths tracks the pre-padding LLM selection and
           Stage 2 source_summary uses it as the priority slice.
  S1-6  - _is_source_file and _is_test_file recognise Java, Go, Rust, Kotlin, C#,
           Ruby, C++, Swift, Scala, PHP, and Shell extensions.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.checkpoint import Checkpoint
from orchestrator.code_search import (
    extract_symbols_from_text,
    search_repository_for_symbols,
)
from orchestrator.models import TicketData
from orchestrator.stages.stage1_context import (
    TicketContext,
    _build_snippet_index,
    _is_source_file,
    _is_test_file,
    _rank_relevant_files,
    _snippet_content_cache,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(**kwargs) -> TicketData:
    defaults = dict(
        ticket_id="TEST-1",
        summary="Fix UserService login",
        description="Login button broken",
        acceptance_criteria="User can log in",
        labels=["SpecDrivenDev"],
    )
    defaults.update(kwargs)
    return TicketData(**defaults)


def _make_llm_mock(response_lines: list) -> MagicMock:
    mock = MagicMock()
    mock.chat.return_value = {"content": "\n".join(response_lines)}
    return mock


def _make_gitlab_mock(file_contents: dict) -> MagicMock:
    mock = MagicMock()

    def _get_content(path, ref="main"):
        if path not in file_contents:
            raise FileNotFoundError(f"Not found: {path}")
        return file_contents[path]

    mock.get_file_content.side_effect = _get_content
    return mock


def _make_tree_blob(path: str) -> MagicMock:
    obj = MagicMock()
    obj.path = path
    obj.type = "blob"
    return obj


def _make_full_mocks(
    test_paths: list,
    source_paths: list,
    llm_response: list = None,
    ticket_data: TicketData = None,
):
    ticket = ticket_data if ticket_data is not None else _make_ticket(summary="Fix UserService login")
    jira = MagicMock()
    jira.get_ticket.return_value = ticket
    all_paths = test_paths + source_paths
    tree = [_make_tree_blob(p) for p in all_paths]
    gitlab = MagicMock()
    gitlab.get_default_branch.return_value = "main"
    gitlab.get_file_tree.return_value = tree
    gitlab.get_file_content.return_value = "// content"
    gitlab.project_id = "proj-1"
    llm = MagicMock()
    llm_lines = llm_response if llm_response is not None else (source_paths[:1] if source_paths else [])
    llm.chat.return_value = {"content": "\n".join(llm_lines)}
    checkpoint = MagicMock()
    checkpoint.current_stage = 0
    checkpoint.repo_id = None
    audit = MagicMock()
    return jira, gitlab, llm, checkpoint, audit


# ===========================================================================
# S1-6 -- Language extension tests
# ===========================================================================

class TestIsSourceFile:
    def test_ts(self):      assert _is_source_file("src/app.ts")
    def test_tsx(self):     assert _is_source_file("src/Button.tsx")
    def test_js(self):      assert _is_source_file("lib/utils.js")
    def test_jsx(self):     assert _is_source_file("components/Card.jsx")
    def test_html(self):    assert _is_source_file("index.html")
    def test_css(self):     assert _is_source_file("style.css")
    def test_scss(self):    assert _is_source_file("theme.scss")
    def test_py(self):      assert _is_source_file("orchestrator/main.py")
    # New languages
    def test_java(self):    assert _is_source_file("src/main/java/UserService.java")
    def test_kotlin(self):  assert _is_source_file("src/main/kotlin/App.kt")
    def test_go(self):      assert _is_source_file("pkg/handler/user.go")
    def test_rust(self):    assert _is_source_file("src/lib.rs")
    def test_c(self):       assert _is_source_file("src/main.c")
    def test_cpp(self):     assert _is_source_file("src/engine.cpp")
    def test_csharp(self):  assert _is_source_file("Services/UserService.cs")
    def test_ruby(self):    assert _is_source_file("app/models/user.rb")
    def test_php(self):     assert _is_source_file("src/Controller.php")
    def test_swift(self):   assert _is_source_file("Sources/App.swift")
    def test_scala(self):   assert _is_source_file("src/main/scala/Main.scala")
    def test_shell(self):   assert _is_source_file("scripts/deploy.sh")
    # Negatives
    def test_json_not_source(self):   assert not _is_source_file("package.json")
    def test_md_not_source(self):     assert not _is_source_file("README.md")
    def test_png_not_source(self):    assert not _is_source_file("logo.png")


class TestIsTestFile:
    def test_ts_test(self):           assert _is_test_file("auth.test.ts")
    def test_ts_spec(self):           assert _is_test_file("auth.spec.tsx")
    def test_js_test(self):           assert _is_test_file("utils.test.js")
    # New conventions
    def test_python_prefix(self):     assert _is_test_file("test_user_service.py")
    def test_python_suffix(self):     assert _is_test_file("user_service_test.py")
    def test_go_test(self):           assert _is_test_file("user_test.go")
    def test_java_test_suffix(self):  assert _is_test_file("UserServiceTest.java")
    def test_java_tests_suffix(self): assert _is_test_file("UserServiceTests.java")
    def test_java_spec_suffix(self):  assert _is_test_file("UserServiceSpec.java")
    def test_kotlin_test(self):       assert _is_test_file("AppTest.kt")
    def test_ruby_spec(self):         assert _is_test_file("user_spec.rb")
    def test_ruby_test(self):         assert _is_test_file("user_test.rb")
    def test_csharp_tests(self):      assert _is_test_file("UserServiceTests.cs")
    def test_csharp_test(self):       assert _is_test_file("UserServiceTest.cs")
    # Negatives
    def test_regular_java_not_test(self): assert not _is_test_file("UserService.java")
    def test_regular_py_not_test(self):   assert not _is_test_file("user_service.py")


# ===========================================================================
# S1-1 -- _build_snippet_index tests
# ===========================================================================

class TestBuildSnippetIndex:
    def setup_method(self):
        _snippet_content_cache.clear()

    def teardown_method(self):
        _snippet_content_cache.clear()

    def test_symbol_hit_anchored_snippet(self):
        content = "\n".join(["line1", "line2", "line3", "UserService here", "line5"])
        gitlab = _make_gitlab_mock({"svc.py": content})
        snippets = _build_snippet_index(["svc.py"], ["UserService"], gitlab, "main")
        assert "svc.py" in snippets
        assert "UserService" in snippets["svc.py"]

    def test_no_symbol_returns_first_n_lines(self):
        content = "\n".join(f"line{i}" for i in range(20))
        gitlab = _make_gitlab_mock({"file.py": content})
        snippets = _build_snippet_index(["file.py"], [], gitlab, "main")
        assert "file.py" in snippets
        assert len(snippets["file.py"].splitlines()) <= 8

    def test_content_cache_populated(self):
        content = "def UserService(): pass"
        gitlab = _make_gitlab_mock({"svc.py": content})
        _build_snippet_index(["svc.py"], ["UserService"], gitlab, "main")
        assert "svc.py" in _snippet_content_cache
        assert _snippet_content_cache["svc.py"] == content

    def test_api_failure_skipped_gracefully(self):
        gitlab = MagicMock()
        gitlab.get_file_content.side_effect = Exception("API error")
        snippets = _build_snippet_index(["broken.py"], ["sym"], gitlab, "main")
        assert snippets == {}

    def test_max_files_to_read_limit(self):
        paths = [f"file{i}.py" for i in range(10)]
        file_contents = {p: f"content {i}" for i, p in enumerate(paths)}
        gitlab = _make_gitlab_mock(file_contents)
        _build_snippet_index(paths, [], gitlab, "main", max_files_to_read=3)
        assert gitlab.get_file_content.call_count == 3


# ===========================================================================
# S1-2 -- _rank_relevant_files symbol-scored padding and low-confidence
# ===========================================================================

class TestRankRelevantFiles:
    def setup_method(self):
        _snippet_content_cache.clear()

    def teardown_method(self):
        _snippet_content_cache.clear()

    def _call(self, llm, paths, symbols=None, gitlab=None, audit=None, max_files=5):
        if gitlab is None:
            gitlab = MagicMock()
            gitlab.get_file_content.return_value = ""
        ticket = _make_ticket()
        return _rank_relevant_files(
            llm=llm,
            ticket=ticket,
            paths=paths,
            gitlab=gitlab,
            branch="main",
            max_files=max_files,
            symbols=symbols or [],
            audit=audit,
        )

    def test_returns_tuple(self):
        llm = _make_llm_mock(["alpha.py"])
        result, llm_selected = self._call(llm, ["alpha.py", "beta.py", "gamma.py"])
        assert isinstance(result, list)
        assert isinstance(llm_selected, list)

    def test_llm_selected_tracks_pre_padding(self):
        llm = _make_llm_mock(["alpha.py"])
        paths = ["alpha.py", "userservice.py", "beta.py", "gamma.py", "delta.py"]
        result, llm_selected = self._call(llm, paths, max_files=4)
        assert llm_selected == ["alpha.py"]
        assert len(result) == 4

    def test_padding_uses_symbol_score_not_alphabetical(self):
        # "userservice.py" matches symbol "UserService", "aaa.py" does not.
        llm = _make_llm_mock([])
        paths = ["aaa.py", "bbb.py", "userservice.py"]
        result, _ = self._call(llm, paths, symbols=["UserService"], max_files=2)
        assert result.index("userservice.py") < result.index("aaa.py")

    def test_low_confidence_warning_audit_logged(self):
        llm = _make_llm_mock([])
        audit = MagicMock()
        paths = ["alpha.py", "beta.py"]
        self._call(llm, paths, audit=audit)
        audit.log.assert_called_once_with(
            stage=1,
            event_type="ranking_low_confidence",
            payload={"llm_selected": 0, "total_candidates": 2},
        )

    def test_no_low_confidence_when_enough_files(self):
        llm = _make_llm_mock(["a.py", "b.py", "c.py"])
        audit = MagicMock()
        self._call(llm, ["a.py", "b.py", "c.py"], audit=audit)
        audit.log.assert_not_called()

    def test_audit_failure_does_not_propagate(self):
        llm = _make_llm_mock([])
        audit = MagicMock()
        audit.log.side_effect = RuntimeError("audit broken")
        result, _ = self._call(llm, ["alpha.py"], audit=audit)
        assert isinstance(result, list)


# ===========================================================================
# S1-3 -- search_repository_for_symbols gitlab-backed path
# ===========================================================================

class TestSearchRepositoryForSymbolsGitlab:
    def test_finds_symbol_via_gitlab(self):
        gitlab = MagicMock()
        gitlab.get_file_content.return_value = "def UserService(): pass"
        hits = search_repository_for_symbols(
            repo_root=".",
            symbols=["UserService"],
            all_file_paths=["svc.py"],
            gitlab=gitlab,
            branch="main",
        )
        assert len(hits) >= 1
        assert hits[0].symbol == "UserService"

    def test_symbol_not_found_via_gitlab(self):
        gitlab = MagicMock()
        gitlab.get_file_content.return_value = "def other_function(): pass"
        hits = search_repository_for_symbols(
            repo_root=".",
            symbols=["NonExistentSymbol"],
            all_file_paths=["svc.py"],
            gitlab=gitlab,
            branch="main",
        )
        assert hits == []

    def test_api_failure_skips_file(self):
        gitlab = MagicMock()
        gitlab.get_file_content.side_effect = Exception("403 Forbidden")
        hits = search_repository_for_symbols(
            repo_root=".",
            symbols=["UserService"],
            all_file_paths=["svc.py"],
            gitlab=gitlab,
            branch="main",
        )
        assert hits == []

    def test_no_all_file_paths_with_gitlab_returns_empty(self):
        gitlab = MagicMock()
        hits = search_repository_for_symbols(
            repo_root=".",
            symbols=["UserService"],
            all_file_paths=None,
            gitlab=gitlab,
            branch="main",
        )
        assert hits == []

    def test_disk_fallback_still_works(self, tmp_path):
        f = tmp_path / "style.css"
        f.write_text(".dark-mode { background: #000; }", encoding="utf-8")
        hits = search_repository_for_symbols(
            repo_root=tmp_path,
            symbols=["dark-mode"],
            all_file_paths=["style.css"],
        )
        assert len(hits) >= 1
        assert hits[0].symbol == "dark-mode"


# ===========================================================================
# S1-4 -- Test file cap in run()
# ===========================================================================

class TestRunTestFileCap:
    def test_test_files_capped_at_max(self):
        test_paths = [f"tests/test_module{i}.py" for i in range(20)]
        source_paths = ["src/user.py"]
        jira, gitlab, llm, checkpoint, audit = _make_full_mocks(test_paths, source_paths)
        context = run(
            ticket_id="TEST-1",
            jira=jira,
            gitlab=gitlab,
            llm=llm,
            checkpoint=checkpoint,
            audit=audit,
            qualifying_label="SpecDrivenDev",
            max_context_test_files=5,
        )
        assert len(context.test_files) <= 5

    def test_test_files_sorted_by_symbol_relevance(self):
        # Use a ticket whose description produces "UserService" via the class regex.
        # This ensures symbol scoring ranks "test_userservice.py" above generic tests.
        ticket = _make_ticket(
            summary="Fix login bug",
            description="class UserService has a login defect",
            acceptance_criteria="User can log in",
        )
        test_paths = [
            "tests/test_util.py",
            "tests/test_other.py",
            "tests/test_userservice.py",
        ]
        source_paths = ["src/user.py"]
        jira, gitlab, llm, checkpoint, audit = _make_full_mocks(test_paths, source_paths)
        jira.get_ticket.return_value = ticket
        context = run(
            ticket_id="TEST-1",
            jira=jira,
            gitlab=gitlab,
            llm=llm,
            checkpoint=checkpoint,
            audit=audit,
            qualifying_label="SpecDrivenDev",
            max_context_test_files=1,
        )
        assert len(context.test_files) == 1
        assert "userservice" in list(context.test_files.keys())[0]


# ===========================================================================
# S1-5 -- TicketContext.llm_selected_paths
# ===========================================================================

class TestTicketContextLLMSelectedPaths:
    def test_default_empty(self):
        ctx = TicketContext(
            ticket=_make_ticket(),
            default_branch="main",
            source_files={},
            test_files={},
            all_file_paths=[],
        )
        assert ctx.llm_selected_paths == []

    def test_explicit_value_preserved(self):
        ctx = TicketContext(
            ticket=_make_ticket(),
            default_branch="main",
            source_files={"a.py": "content"},
            test_files={},
            all_file_paths=["a.py"],
            llm_selected_paths=["a.py"],
        )
        assert ctx.llm_selected_paths == ["a.py"]

    def test_run_populates_llm_selected_paths_for_small_repo(self):
        source_paths = ["src/a.py", "src/b.py"]
        jira, gitlab, llm, checkpoint, audit = _make_full_mocks([], source_paths)
        context = run(
            ticket_id="TEST-1",
            jira=jira,
            gitlab=gitlab,
            llm=llm,
            checkpoint=checkpoint,
            audit=audit,
            qualifying_label="SpecDrivenDev",
        )
        assert set(context.llm_selected_paths) == set(source_paths)

    def test_run_audit_log_includes_llm_split(self):
        source_paths = ["src/a.py"]
        jira, gitlab, llm, checkpoint, audit = _make_full_mocks([], source_paths)
        run(
            ticket_id="TEST-1",
            jira=jira,
            gitlab=gitlab,
            llm=llm,
            checkpoint=checkpoint,
            audit=audit,
            qualifying_label="SpecDrivenDev",
        )
        complete_call = None
        for c in audit.log.call_args_list:
            kw = c.kwargs if c.kwargs else {}
            event = kw.get("event_type")
            if event == "stage1_complete":
                complete_call = c
                break
        assert complete_call is not None
        payload = complete_call.kwargs.get("payload") or {}
        assert "llm_selected_count" in payload
        assert "symbol_padded_count" in payload


# ===========================================================================
# S1-1 -- Content cache avoids duplicate API calls
# ===========================================================================

class TestContentCacheAvoidsDuplicateFetches:
    def test_no_double_fetch(self):
        """Files pre-fetched by the snippet index must not be re-fetched in run()."""
        source_paths = [f"src/module{i}.py" for i in range(15)]
        jira, gitlab, llm, checkpoint, audit = _make_full_mocks(
            [], source_paths, llm_response=source_paths[:3]
        )
        run(
            ticket_id="TEST-1",
            jira=jira,
            gitlab=gitlab,
            llm=llm,
            checkpoint=checkpoint,
            audit=audit,
            qualifying_label="SpecDrivenDev",
        )
        fetched = [c.args[0] for c in gitlab.get_file_content.call_args_list]
        for path in source_paths[:3]:
            assert fetched.count(path) <= 1, f"{path} was fetched more than once"


# ===========================================================================
# Logical Edge-Case Tests (Post-Fix Hardening)
# ===========================================================================

import threading


class TestLogicalEdgeCasesThreadSafetyAndCleanup:
    def test_thread_local_cache_isolation(self):
        """Concurrent threads must not see or overwrite each other's cached file content."""
        results = {}

        def thread_1_work():
            _snippet_content_cache["repo1_file.py"] = "content_repo1"
            import time
            time.sleep(0.05)
            results["t1_sees_t1"] = "repo1_file.py" in _snippet_content_cache
            results["t1_sees_t2"] = "repo2_file.py" in _snippet_content_cache
            results["t1_val"] = _snippet_content_cache.get("repo1_file.py")

        def thread_2_work():
            _snippet_content_cache["repo2_file.py"] = "content_repo2"
            import time
            time.sleep(0.05)
            results["t2_sees_t2"] = "repo2_file.py" in _snippet_content_cache
            results["t2_sees_t1"] = "repo1_file.py" in _snippet_content_cache
            results["t2_val"] = _snippet_content_cache.get("repo2_file.py")

        t1 = threading.Thread(target=thread_1_work)
        t2 = threading.Thread(target=thread_2_work)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["t1_sees_t1"] is True
        assert results["t1_sees_t2"] is False
        assert results["t1_val"] == "content_repo1"

        assert results["t2_sees_t2"] is True
        assert results["t2_sees_t1"] is False
        assert results["t2_val"] == "content_repo2"

    def test_cache_cleared_on_exception_in_run(self):
        """If run() encounters an unhandled exception midway, the cache must be cleared via finally."""
        jira = MagicMock()
        jira.get_ticket.side_effect = RuntimeError("Fatal network error")
        gitlab = MagicMock()
        llm = MagicMock()
        checkpoint = Checkpoint(ticket_id="TICKET-ERR")
        audit = MagicMock()

        _snippet_content_cache["stale.py"] = "should be cleared"
        with pytest.raises(RuntimeError, match="Fatal network error"):
            run(
                ticket_id="TICKET-ERR",
                jira=jira,
                gitlab=gitlab,
                llm=llm,
                checkpoint=checkpoint,
                audit=audit,
                qualifying_label="SpecDrivenDev",
            )
        assert len(_snippet_content_cache) == 0


class TestSnippetCandidatePrioritization:
    def test_candidate_paths_with_symbol_in_name_prioritized(self):
        """
        When there are more candidate files than max_files_to_read, files with
        symbols in their path must be read first, even if alphabetically last.
        """
        # Create 10 files: file0..file8 without symbol, file9 with symbol
        paths = [f"src/file{i}.py" for i in range(9)] + ["src/user_service.py"]
        contents = {p: f"content of {p}" for p in paths}
        gitlab = _make_gitlab_mock(contents)

        # Limit to 3 files to read
        snippets = _build_snippet_index(
            paths, ["user_service"], gitlab, "main", max_files_to_read=3
        )
        assert "src/user_service.py" in snippets
        assert "src/user_service.py" in _snippet_content_cache


class TestMultiLanguageSymbolExtraction:
    def test_extract_symbols_multi_language_paths(self):
        """Ticket text with Java, Go, Rust, C#, Kotlin paths must be extracted as symbols."""
        text = (
            "Please fix the bug in src/main/java/com/example/UserService.java and "
            "pkg/handler/user.go as well as src/service/order.rs and Model.cs"
        )
        symbols = extract_symbols_from_text(text)
        assert "src/main/java/com/example/UserService.java" in symbols
        assert "pkg/handler/user.go" in symbols
        assert "src/service/order.rs" in symbols


class TestSearchRepositoryForSymbolsProtections:
    def test_early_break_when_all_symbols_satisfied(self):
        """Search must exit early as soon as all symbols reach max_results_per_symbol."""
        gitlab = MagicMock()
        # Returns content containing the symbol for every file
        gitlab.get_file_content.return_value = "def my_symbol(): pass"

        paths = [f"src/file{i}.py" for i in range(50)]
        hits = search_repository_for_symbols(
            repo_root=".",
            symbols=["my_symbol"],
            all_file_paths=paths,
            max_results_per_symbol=2,
            gitlab=gitlab,
            branch="main",
        )
        assert len(hits) == 2
        # Must have exited early after 2 hits, not checked all 50 files!
        assert gitlab.get_file_content.call_count == 2

    def test_api_scan_cap_applied(self):
        """In CI mode, candidate paths scanned via API must not exceed max_api_scans."""
        gitlab = MagicMock()
        gitlab.get_file_content.return_value = "nothing relevant here"

        paths = [f"src/file{i}.py" for i in range(100)]
        search_repository_for_symbols(
            repo_root=".",
            symbols=["nonexistent"],
            all_file_paths=paths,
            max_results_per_symbol=5,
            gitlab=gitlab,
            branch="main",
            max_api_scans=15,
        )
        assert gitlab.get_file_content.call_count <= 15


class TestPlainEnglishKeywordFallback:
    def test_plain_english_ticket_sorts_test_files(self):
        """A ticket without code syntax (class/def) should still prioritize test files matching summary words."""
        test_paths = ["tests/test_unrelated.py", "tests/test_checkout.py", "tests/test_zebra.py"]
        source_paths = ["src/checkout.py"]
        # Summary has 'checkout' as a keyword
        ticket_data = TicketData(
            ticket_id="TICKET-PLAIN",
            summary="Checkout flow fails when discount applied",
            description="Users report issue at checkout screen",
            acceptance_criteria="Checkout completes successfully",
            labels=["SpecDrivenDev"],
        )
        jira, gitlab, llm, checkpoint, audit = _make_full_mocks(
            test_paths, source_paths, ticket_data=ticket_data
        )
        context = run(
            ticket_id="TICKET-PLAIN",
            jira=jira,
            gitlab=gitlab,
            llm=llm,
            checkpoint=checkpoint,
            audit=audit,
            qualifying_label="SpecDrivenDev",
            max_context_test_files=2,
        )
        # tests/test_checkout.py should be included because 'checkout' was extracted from summary
        assert "tests/test_checkout.py" in context.test_files


