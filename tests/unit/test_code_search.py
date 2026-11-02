"""Unit tests for orchestrator.code_search (Phase 2)."""
import pathlib
import pytest
from orchestrator.code_search import (
    ContextExpansionResult,
    SearchHit,
    extract_symbols_from_text,
    search_repository_for_symbols,
)


class TestExtractSymbols:
    def test_css_class(self):
        syms = extract_symbols_from_text("The .dark-mode class should be applied")
        assert "dark-mode" in syms

    def test_css_id(self):
        syms = extract_symbols_from_text("Update the #main-nav element")
        assert "main-nav" in syms

    def test_python_function(self):
        syms = extract_symbols_from_text("Fix the def calculate_total function")
        assert "calculate_total" in syms

    def test_file_path(self):
        syms = extract_symbols_from_text("Update src/components/Header.tsx")
        assert "src/components/Header.tsx" in syms

    def test_no_symbols(self):
        syms = extract_symbols_from_text("Do something general")
        assert isinstance(syms, list)

    def test_short_symbols_ignored(self):
        syms = extract_symbols_from_text("Use .ab class")
        assert "ab" not in syms  # too short (< 3 chars)


class TestSearchRepositoryForSymbols:
    def test_finds_symbol_in_file(self, tmp_path):
        f = tmp_path / "style.css"
        f.write_text(".dark-mode { background: #000; }", encoding="utf-8")
        hits = search_repository_for_symbols(
            repo_root=tmp_path,
            symbols=["dark-mode"],
            all_file_paths=["style.css"],
        )
        assert len(hits) >= 1
        assert hits[0].symbol == "dark-mode"
        assert "style.css" in hits[0].path

    def test_symbol_not_found(self, tmp_path):
        f = tmp_path / "style.css"
        f.write_text("body { color: red; }", encoding="utf-8")
        hits = search_repository_for_symbols(
            repo_root=tmp_path,
            symbols=["nonexistent-symbol"],
            all_file_paths=["style.css"],
        )
        assert hits == []

    def test_max_results_respected(self, tmp_path):
        for i in range(5):
            f = tmp_path / f"file{i}.css"
            f.write_text(".target { color: red; }", encoding="utf-8")
        hits = search_repository_for_symbols(
            repo_root=tmp_path,
            symbols=["target"],
            max_results_per_symbol=2,
        )
        assert len(hits) <= 2
