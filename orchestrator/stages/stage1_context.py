"""
Stage 1 — Assemble Ticket Context.

Pulls ticket data from the issue tracker (GitHub Issues or JIRA) and
the relevant source/test files from the VCS platform (GitHub or GitLab).
Produces a ``TicketContext`` dataclass consumed by Stage 2.

Implements FR-3, FR-4.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from orchestrator.audit import AuditLogger
from orchestrator.checkpoint import Checkpoint
from orchestrator.code_search import (
    extract_identifiers_from_content,
    extract_symbols_from_text,
    find_additional_context_files,
    rank_by_bm25,
    search_repository_for_symbols,
    trace_import_graph,
)
from orchestrator.jira_client import TicketData
from orchestrator.llm_client import LLMClient
from orchestrator.repo_resolver import RepoResolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# S1-6 FIX — Expanded test-file glob patterns to cover non-JS/TS conventions.
# ---------------------------------------------------------------------------
_TEST_PATTERNS = (
    # JavaScript / TypeScript
    "*.test.ts",
    "*.test.tsx",
    "*.spec.ts",
    "*.spec.tsx",
    "*.test.js",
    "*.test.jsx",
    "*.spec.js",
    "*.spec.jsx",
    # Python (pytest / unittest)
    "test_*.py",
    "*_test.py",
    # Go
    "*_test.go",
    # Java / Kotlin
    "*Test.java",
    "*Tests.java",
    "*Spec.java",
    "*Test.kt",
    # Ruby
    "*_spec.rb",
    "*_test.rb",
    # C#
    "*Tests.cs",
    "*Test.cs",
    # Rust tests are inline modules — no standalone file convention.
)

# ---------------------------------------------------------------------------
# S1-6 FIX — Expanded source-file extensions to include Java, Go, Rust, etc.
# ---------------------------------------------------------------------------
_SOURCE_EXTENSIONS = (
    # JavaScript / TypeScript
    "*.ts",
    "*.tsx",
    "*.js",
    "*.jsx",
    # Web / Styles
    "*.html",
    "*.htm",
    "*.css",
    "*.scss",
    "*.sass",
    # Python
    "*.py",
    # Java / Kotlin / Groovy
    "*.java",
    "*.kt",
    "*.kts",
    "*.groovy",
    # Go
    "*.go",
    # Rust
    "*.rs",
    # C / C++
    "*.c",
    "*.cpp",
    "*.cc",
    "*.h",
    "*.hpp",
    # C#
    "*.cs",
    # Ruby
    "*.rb",
    # PHP
    "*.php",
    # Swift
    "*.swift",
    # Scala
    "*.scala",
    # Shell scripts
    "*.sh",
    "*.bash",
)

_IGNORE_DIRS = (
    "node_modules",
    "dist",
    "build",
    ".git",
    "coverage",
    "__pycache__",
)


class _ThreadLocalDict(dict):
    """
    A thread-safe dict proxy that keeps separate storage per thread while
    maintaining standard dict interface for tests and module-level access.
    Prevents cache collision and race conditions during concurrent runs.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._local = threading.local()

    def _get_data(self) -> dict[str, str]:
        if not hasattr(self._local, "data"):
            self._local.data = {}
        return self._local.data

    def __getitem__(self, key: str) -> str:
        return self._get_data()[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._get_data()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._get_data()[key]

    def __contains__(self, key: object) -> bool:
        return key in self._get_data()

    def get(self, key: str, default: Any = None) -> Any:
        return self._get_data().get(key, default)

    def clear(self) -> None:
        self._get_data().clear()

    def __len__(self) -> int:
        return len(self._get_data())

    def __iter__(self):
        return iter(self._get_data())

    def items(self):
        return self._get_data().items()

    def keys(self):
        return self._get_data().keys()

    def values(self):
        return self._get_data().values()


# ---------------------------------------------------------------------------
# S1-1 FIX — Thread-isolated content cache so _build_snippet_index pre-fetches
# don't result in duplicate API calls when the run() loop later reads the
# same files, with zero thread contention or cross-ticket state leakage.
# ---------------------------------------------------------------------------
_snippet_content_cache: _ThreadLocalDict = _ThreadLocalDict()

# ---------------------------------------------------------------------------
# S1-2 FIX — Minimum number of files the LLM must return before a
# low-confidence warning is emitted.
# ---------------------------------------------------------------------------
_LLM_MIN_RETURN_THRESHOLD = 3


@dataclass
class TicketContext:
    """Everything the downstream stages need to know about a ticket."""

    ticket: TicketData
    default_branch: str
    source_files: dict[str, str]  # {path: content}
    test_files: dict[str, str]    # {path: content}
    all_file_paths: list[str]     # full tree listing (paths only)
    # S1-5 FIX — paths selected by the LLM before symbol-scored padding.
    # Downstream stages use these as their priority-15 slice instead of
    # relying on dict-insertion order (which mixes LLM-selected and padded).
    llm_selected_paths: list[str] = field(default_factory=list)


_INELIGIBLE_KEYWORDS = (
    ("database migration", "Database schema migration detected"),
    ("db migration", "Database schema migration detected"),
    ("alter table", "SQL/schema alteration detected"),
    ("schema overhaul", "Database schema changes detected"),
    ("jwt token authentication overhaul", "Security/Auth core overhaul detected"),
    ("cross-repo", "Multi-repository modification not supported"),
    ("multi-repo", "Multi-repository modification not supported"),
)


def _check_qualification(ticket: TicketData) -> str | None:
    """Return reason if ticket is ineligible for automation, or None if eligible."""
    content = f"{ticket.summary} {ticket.description}".lower()
    for kw, reason in _INELIGIBLE_KEYWORDS:
        if kw in content:
            return reason
    if not ticket.acceptance_criteria or not ticket.acceptance_criteria.strip():
        return "Missing acceptance criteria"
    return None


def run(
    ticket_id: str,
    jira: Any,
    gitlab: Any,
    llm: LLMClient,
    checkpoint: Checkpoint,
    audit: AuditLogger,
    qualifying_label: str,
    repo_resolver: RepoResolver | None = None,
    max_context_test_files: int = 10,
) -> TicketContext:
    """
    Execute Stage 1: fetch ticket data, enumerate repo, fetch relevant files.

    Parameters
    ----------
    ticket_id : str
        The ticket identifier (e.g. ``PROJ-123`` or ``42``).
    jira : Any
        Initialised issue tracker client (``GitHubIssueClient`` or ``JiraClient``).
    gitlab : Any
        Initialised VCS client (``GitHubClient`` or ``GitLabClient``).
    llm : LLMClient
        Initialised LLM client (used for relevance ranking).
    checkpoint : Checkpoint
        Current checkpoint (updated on completion).
    audit : AuditLogger
        Audit log writer.
    qualifying_label : str
        The label the ticket must carry to be eligible.
    repo_resolver : RepoResolver | None
        Optional repository resolver for dynamic multi-repo routing (Phase 5).
    max_context_test_files : int
        S1-4 — Maximum number of test files to include in context.  Defaults to
        10; pass ``settings.max_context_test_files`` from the caller to make it
        configurable via environment variable.

    Returns
    -------
    TicketContext
    """
    # Always start with a clean per-run content cache.
    _snippet_content_cache.clear()

    audit.log(stage=1, event_type="stage1_start")

    # ── 1. Fetch ticket ─────────────────────────────────────────────────
    logger.info("Fetching ticket %s…", ticket_id)
    ticket = jira.get_ticket(ticket_id)
    logger.info("Ticket %s: %s", ticket.ticket_id, ticket.summary)

    if qualifying_label not in ticket.labels:
        raise ValueError(
            f"Ticket {ticket_id} does not carry the qualifying label "
            f"'{qualifying_label}'.  Labels present: {ticket.labels}"
        )

    # ── Qualification triage check (Phase 4) ───────────────────────────
    disqualification_reason = _check_qualification(ticket)
    if disqualification_reason:
        logger.warning(
            "Ticket %s failed qualification triage: %s",
            ticket_id,
            disqualification_reason,
        )
        jira.post_lifecycle_comment(
            ticket_id=ticket_id,
            title="Triage: Ineligible for Automation",
            message=(
                f"This ticket was flagged with `{qualifying_label}` but is "
                f"ineligible for spec-driven automation:\n"
                f"**Reason:** {disqualification_reason}\n\n"
                f"The label `{qualifying_label}` has been removed."
            ),
        )
        jira.remove_label(ticket_id, qualifying_label)
        audit.log(
            stage=1,
            event_type="stage1_triaged_ineligible",
            payload={"reason": disqualification_reason},
        )
        raise ValueError(
            f"Ticket {ticket_id} is ineligible for automation: {disqualification_reason}"
        )

    # ── Multi-repo resolution (Phase 5) ─────────────────────────────────
    if repo_resolver:
        default_id = gitlab.project_id or ""
        target_repo_id = repo_resolver.resolve(ticket, default_repo_id=default_id)
        if target_repo_id and str(target_repo_id) != str(gitlab.project_id):
            logger.info(
                "Switching repository from %s to %s for ticket %s",
                gitlab.project_id,
                target_repo_id,
                ticket_id,
            )
            gitlab.switch_project(target_repo_id)
            checkpoint.repo_id = target_repo_id
        elif gitlab.project_id:
            checkpoint.repo_id = gitlab.project_id

    # ── 2. Enumerate repository file tree ───────────────────────────────
    logger.info("Enumerating repository file tree…")
    default_branch = gitlab.get_default_branch()
    tree = gitlab.get_file_tree(ref=default_branch, recursive=True)
    all_paths = [f.path for f in tree if f.type == "blob"]

    # Filter out ignored directories.
    all_paths = [
        p for p in all_paths
        if not any(p.startswith(d + "/") or p.startswith(d + "\\") for d in _IGNORE_DIRS)
    ]

    # ── 3. Separate test files from source files ────────────────────────
    test_paths = [p for p in all_paths if _is_test_file(p)]
    source_paths = [p for p in all_paths if _is_source_file(p) and p not in test_paths]

    logger.info("Found %d files: %d source, %d test",
                len(all_paths), len(source_paths), len(test_paths))

    # ── 4. Extract ticket symbols (shared by multiple steps below) ──────
    ticket_text = f"{ticket.summary} {ticket.description} {ticket.acceptance_criteria}"
    symbols = extract_symbols_from_text(ticket_text)
    if not symbols and ticket.summary:
        # Fallback for plain-English tickets without explicit code syntax:
        # extract significant alphanumeric words (>=4 chars) from summary.
        stop_words = {
            "this", "that", "with", "from", "when", "then", "have", "been",
            "will", "should", "fix", "issue", "bug", "error", "fail", "fails",
            "cant", "does", "cant", "must", "user", "page", "button",
        }
        words = [w.strip(".,;:()[]'\"`").lower() for w in ticket.summary.split()]
        symbols = [w for w in words if len(w) >= 4 and w not in stop_words]

    if symbols:
        logger.info("Extracted %d symbols from ticket: %s", len(symbols), symbols[:15])

    # ── 5. Fetch test files — S1-4 FIX: rank by symbol relevance first ─
    # On large repos with hundreds of test files, fetching all of them burns
    # API quota and overflows the downstream LLM context window.  Instead,
    # sort by how many ticket symbols appear in the path and take the top N.
    ranked_test_paths = _sort_by_symbol_relevance(test_paths, symbols)
    n_test = min(max_context_test_files, len(ranked_test_paths))
    logger.info(
        "Fetching top %d of %d test file(s) by symbol relevance…",
        n_test,
        len(ranked_test_paths),
    )
    test_files: dict[str, str] = {}
    for path in ranked_test_paths[:max_context_test_files]:
        try:
            test_files[path] = gitlab.get_file_content(path, ref=default_branch)
        except Exception as exc:
            logger.warning("Could not fetch test file %s: %s", path, exc)

    skipped_tests = len(test_paths) - len(test_files)
    if skipped_tests > 0:
        logger.info(
            "Skipped %d lower-relevance test file(s) to preserve context budget.",
            skipped_tests,
        )

    # ── 6. Rank source files by relevance to the ticket ────────────────
    # Issue 8 fix: Always run symbol extraction + LLM ranking regardless of
    # repo size. The old "<= 30 files → send everything" path caused the LLM
    # to receive all files with zero filtering, burning token budget and
    # confusing it with irrelevant files.
    #
    # S1-5: Track the LLM-selected set before any padding is applied.
    try:
        if len(source_paths) <= 10:
            # Very small repos: take all files but still sort by symbol relevance.
            relevant_paths = _sort_by_symbol_relevance(source_paths, symbols)
            # For tiny repos every file is "LLM-equivalent" — no padding needed.
            llm_selected_paths: list[str] = list(relevant_paths)
        else:
            logger.info(
                "Running symbol scan + LLM ranking on %d source file(s)…", len(source_paths)
            )
            # S1-1 FIX: pass gitlab + branch so the ranking call can read file
            # content snippets and make content-aware decisions.
            relevant_paths, llm_selected_paths = _rank_relevant_files(
                llm, ticket, source_paths,
                gitlab=gitlab,
                branch=default_branch,
                max_files=30,
                symbols=symbols,
                audit=audit,
            )

        logger.info("Fetching %d relevant source file(s)…", len(relevant_paths))
        # Issue 9 fix: Preserve relevance order when building the dict.
        # downstream [:15] slices now get the TOP-ranked files, not the
        # alphabetically-first files.
        #
        # S1-1 FIX: Use the content cache populated by _build_snippet_index to
        # avoid re-fetching files that were already read during ranking.
        source_files: dict[str, str] = {}
        for path in relevant_paths:
            if path in _snippet_content_cache:
                source_files[path] = _snippet_content_cache[path]
            else:
                try:
                    source_files[path] = gitlab.get_file_content(path, ref=default_branch)
                except Exception as exc:
                    logger.warning("Could not fetch source file %s: %s", path, exc)

        # ── 7. Build context and update checkpoint ─────────────────────────
        context = TicketContext(
            ticket=ticket,
            default_branch=default_branch,
            source_files=source_files,
            test_files=test_files,
            all_file_paths=all_paths,
            llm_selected_paths=llm_selected_paths,  # S1-5
        )

        if checkpoint.current_stage < 2:
            checkpoint.current_stage = 2

        # S1-5 FIX: emit LLM-selected vs symbol-padded split in the audit log.
        audit.log(
            stage=1,
            event_type="stage1_complete",
            payload={
                "source_file_count": len(source_files),
                "llm_selected_count": len(llm_selected_paths),
                "symbol_padded_count": len(source_files) - len(llm_selected_paths),
                "test_file_count": len(test_files),
                "total_files": len(all_paths),
                "repo_id": checkpoint.repo_id,
            },
        )
        logger.info("Stage 1 complete for %s", ticket_id)
        return context
    finally:
        # Clear cache once fetching is done (or on failure) to free memory.
        _snippet_content_cache.clear()


# ── Helpers ─────────────────────────────────────────────────────────────


def _is_test_file(path: str) -> bool:
    """Check whether *path* matches any known test-file glob (all languages)."""
    basename = path.rsplit("/", 1)[-1] if "/" in path else path
    return any(fnmatch.fnmatch(basename, pat) for pat in _TEST_PATTERNS)


def _is_source_file(path: str) -> bool:
    """Check whether *path* is a recognized source file (multi-language)."""
    basename = path.rsplit("/", 1)[-1] if "/" in path else path
    return any(fnmatch.fnmatch(basename, pat) for pat in _SOURCE_EXTENSIONS)


def _sort_by_symbol_relevance(paths: list[str], symbols: list[str]) -> list[str]:
    """
    Issue 8/9 helper: Sort file paths so that files whose names/paths contain
    ticket symbols (CSS class names, function names, component IDs) come first.
    Files with no symbol match retain their original order at the end.
    Used for very small repos where LLM ranking would be overkill.
    """
    if not symbols:
        return list(paths)
    symbols_lower = [s.lower() for s in symbols]

    def score(path: str) -> int:
        p_lower = path.lower()
        return sum(1 for s in symbols_lower if s in p_lower)

    return sorted(paths, key=score, reverse=True)



# ---------------------------------------------------------------------------
# S1-1 FIX — Content-snippet index builder
# ---------------------------------------------------------------------------

def _build_snippet_index(
    paths: list[str],
    symbols: list[str],
    gitlab: Any,
    branch: str,
    max_files_to_read: int = 60,
    max_snippet_lines: int = 8,
) -> dict[str, str]:
    """
    For each candidate path (up to *max_files_to_read*), fetch file content
    via the VCS API and extract a short snippet.  If a symbol from the ticket
    appears in the file, the snippet is anchored around that hit.  Otherwise,
    the first *max_snippet_lines* lines are returned as a generic preview.

    As a side-effect, populates ``_snippet_content_cache`` with the **full**
    file content so the subsequent source-file fetch loop in ``run()`` can
    reuse it without an extra API round-trip.

    Returns
    -------
    dict[str, str]
        ``{path: snippet_text}``
    """
    snippets: dict[str, str] = {}
    symbols_lower = [s.lower() for s in symbols]

    # Prioritize candidate files whose paths contain extracted symbols so that
    # the reading budget is spent on high-probability files rather than
    # purely alphabetical prefixes.
    candidate_paths = _sort_by_symbol_relevance(paths, symbols)[:max_files_to_read]

    for path in candidate_paths:
        try:
            content = gitlab.get_file_content(path, ref=branch)
        except Exception:
            continue

        # Cache full content for reuse in the source-file fetch loop.
        _snippet_content_cache[path] = content

        lines = content.splitlines()
        if symbols_lower:
            hit_lines = [
                (i, line) for i, line in enumerate(lines, 1)
                if any(sym in line.lower() for sym in symbols_lower)
            ]
        else:
            hit_lines = []

        if hit_lines:
            first_hit_lineno = hit_lines[0][0]
            start = max(0, first_hit_lineno - 2)
            end = min(len(lines), first_hit_lineno + max_snippet_lines)
            snippet = "\n".join(lines[start:end])
            snippets[path] = f"[Lines {start + 1}-{end}]\n{snippet}"
        else:
            snippets[path] = "\n".join(lines[:max_snippet_lines])

    return snippets


# ---------------------------------------------------------------------------
# S1-1 + S1-2 + S1-5 FIX — Enriched LLM ranking with content previews,
# symbol-scored padding, low-confidence warning, and pre-padding tracking.
# ---------------------------------------------------------------------------

def _rank_relevant_files(
    llm: LLMClient,
    ticket: TicketData,
    paths: list[str],
    gitlab: Any,
    branch: str,
    max_files: int = 30,
    symbols: list[str] | None = None,
    audit: Any = None,
) -> tuple[list[str], list[str]]:
    """
    Use the LLM to pick the most relevant source files for the ticket.

    Returns
    -------
    tuple[list[str], list[str]]
        ``(final_paths, llm_selected_paths)`` where *llm_selected_paths* is
        the subset returned directly by the LLM (before any padding) and
        *final_paths* is the fully-expanded list ready for fetching.

    Improvements vs. original
    -------------------------
    * S1-1: Content-snippet index via ``_build_snippet_index()`` injects
      file previews so the LLM makes content-aware decisions.
    * S1-2: Symbol-scored padding replaces alphabetical order; low-confidence
      warning when LLM returns too few files.
    * S1-5: Pre-padding LLM selection stored as ``llm_selected_paths``.
    * GAP-1 (BM25): BM25 pre-ranks all candidate paths against the ticket
      text *before* building the snippet index, so the 60 files we read
      and show to the LLM are the textually most relevant, not alphabetical.
    * GAP-2 (Import graph): After LLM selection, parse import statements to
      pull in direct dependencies that must also change.
    * GAP-4 (Iterative refinement): Extract identifiers from top-ranked
      files and search remaining repo for files that reference them,
      surfacing callers/consumers that the LLM may have missed.
    """
    ticket_text = f"{ticket.summary} {ticket.description} {ticket.acceptance_criteria}"
    if symbols is None:
        symbols = extract_symbols_from_text(ticket_text)

    symbol_hint = ""
    if symbols:
        symbol_hint = (
            f"\n## Extracted Symbols (CSS classes, function names, components from ticket)\n"
            f"{', '.join(symbols[:30])}\n"
            f"Give higher priority to files that contain these symbols.\n"
        )

    # ── GAP-1: BM25 pre-ranking ─────────────────────────────────────────
    # Before reading snippets, score all candidate paths via BM25 against the
    # full ticket text. We use path strings as the "document" for paths we
    # haven't fetched yet, then replace with snippet content once available.
    # This ensures the 60-file reading budget is spent on the best candidates.
    path_as_doc = {p: p.replace("/", " ").replace("_", " ").replace(".", " ") for p in paths}
    bm25_ranked = rank_by_bm25(ticket_text, path_as_doc, top_n=min(len(paths), 120))
    # Reorder paths by BM25 score; paths not in bm25_ranked go to the end.
    bm25_set = set(bm25_ranked)
    paths_for_snippets = bm25_ranked + [p for p in paths if p not in bm25_set]
    logger.info(
        "BM25 pre-ranking: reordered %d candidate paths for snippet budget.",
        len(paths_for_snippets),
    )

    # S1-1: Build content snippet index (pre-fetches up to 60 files, now
    # reading the BM25-top files first instead of alphabetical order).
    snippet_index = _build_snippet_index(paths_for_snippets, symbols, gitlab, branch)

    # Re-rank snippets with BM25 now that we have actual content
    if snippet_index:
        snippet_bm25 = rank_by_bm25(ticket_text, snippet_index, top_n=len(snippet_index))
        # Build a priority-order list: BM25-snippet-ranked first, then remaining
        snippet_set = set(snippet_bm25)
        ordered_paths = snippet_bm25 + [p for p in paths_for_snippets if p not in snippet_set]
        logger.info(
            "BM25 content re-ranking: top 5 files are %s",
            ordered_paths[:5],
        )
    else:
        ordered_paths = paths_for_snippets

    # Build the paths block — inject previews where available.
    paths_text_lines = []
    for p in ordered_paths:
        if p in snippet_index:
            preview = snippet_index[p][:300].replace("\n", "\n    ")
            paths_text_lines.append(f"{p}\n    Preview:\n    {preview}")
        else:
            paths_text_lines.append(p)
    paths_text = "\n\n".join(paths_text_lines)

    prompt = (
        "You are a code analysis assistant. Given the following ticket "
        "and a list of repository files with content previews, "
        "return the file paths most likely to need changes to resolve this ticket.\n\n"
        f"## Ticket\n"
        f"**Summary:** {ticket.summary}\n"
        f"**Description:** {ticket.description}\n"
        f"**Acceptance Criteria:** {ticket.acceptance_criteria}\n\n"
        f"{symbol_hint}"
        f"## File Paths and Previews\n```\n{paths_text}\n```\n\n"
        f"Return up to {max_files} file paths, one per line, ranked most-to-least "
        f"relevant. Prioritize files whose preview content directly relates to the "
        f"ticket. No explanation, no numbering, only paths from the list above."
    )

    response = llm.chat(
        prompt,
        stage=1,
        call_purpose="Rank relevant source files (BM25 + content-aware)",
        attached_files=ordered_paths[:60],
    )
    content = response.get("content", "")

    selected: list[str] = []
    valid_set = set(paths)
    for line in content.strip().splitlines():
        line = line.strip().strip("`").strip()
        if line in valid_set:
            selected.append(line)
        if len(selected) >= max_files:
            break

    # Track the LLM-returned subset before any expansion (S1-5).
    llm_selected = list(selected)

    # S1-2 FIX: emit low-confidence warning when the LLM returns too few files.
    min_expected = min(_LLM_MIN_RETURN_THRESHOLD, len(paths))
    if len(llm_selected) < min_expected:
        logger.warning(
            "LLM returned only %d file(s) (expected at least %d) — possible poor "
            "ticket description or ambiguous file tree. Results may be inaccurate.",
            len(llm_selected),
            min_expected,
        )
        if audit is not None:
            try:
                audit.log(
                    stage=1,
                    event_type="ranking_low_confidence",
                    payload={
                        "llm_selected": len(llm_selected),
                        "total_candidates": len(paths),
                    },
                )
            except Exception:
                pass

    # ── GAP-2: Import graph expansion ───────────────────────────────────
    # After LLM ranking, parse imports from selected files to pull in
    # direct dependencies that must also change. Content for selected files
    # is already available in the snippet cache.
    selected_with_content = {
        p: _snippet_content_cache.get(p, snippet_index.get(p, ""))
        for p in selected
    }
    import_deps = trace_import_graph(
        selected_files=selected_with_content,
        all_file_paths=paths,
        max_depth=1,
        max_new_files=10,
    )
    selected_set = set(selected)
    newly_from_imports = [p for p in import_deps if p not in selected_set]
    if newly_from_imports:
        logger.info(
            "Import graph: added %d dependency file(s): %s",
            len(newly_from_imports), newly_from_imports,
        )
        selected.extend(newly_from_imports)
        selected_set.update(newly_from_imports)
        if audit is not None:
            try:
                audit.log(
                    stage=1,
                    event_type="import_graph_expansion",
                    payload={"added_files": newly_from_imports},
                )
            except Exception:
                pass

    # ── GAP-4: Iterative context refinement (second-pass grep) ──────────
    # Extract exported identifiers from the top-ranked files, then search
    # the remaining repo for files that reference those identifiers.
    # This surfaces callers/consumers the LLM may have missed.
    top_identifiers: list[str] = []
    for p in llm_selected[:8]:  # inspect top 8 LLM-chosen files
        cached_content = _snippet_content_cache.get(p, "")
        if cached_content:
            top_identifiers.extend(extract_identifiers_from_content(cached_content))
    # De-duplicate and cap
    seen_ids: set[str] = set()
    unique_identifiers: list[str] = []
    for ident in top_identifiers:
        if ident not in seen_ids:
            seen_ids.add(ident)
            unique_identifiers.append(ident)
    unique_identifiers = unique_identifiers[:20]

    if unique_identifiers:
        logger.info(
            "Iterative refinement: searching for %d identifier(s) across remaining repo: %s",
            len(unique_identifiers), unique_identifiers[:10],
        )
        refinement_hits = search_repository_for_symbols(
            repo_root=".",
            symbols=unique_identifiers,
            all_file_paths=[p for p in paths if p not in selected_set],
            gitlab=gitlab,
            branch=branch,
            max_api_scans=30,
        )
        newly_from_refinement: list[str] = []
        for hit in refinement_hits:
            if hit.path not in selected_set:
                newly_from_refinement.append(hit.path)
                selected_set.add(hit.path)
        if newly_from_refinement:
            logger.info(
                "Iterative refinement: added %d caller/consumer file(s): %s",
                len(newly_from_refinement), newly_from_refinement,
            )
            selected.extend(newly_from_refinement)
            if audit is not None:
                try:
                    audit.log(
                        stage=1,
                        event_type="iterative_refinement",
                        payload={"identifiers_searched": unique_identifiers[:10],
                                 "added_files": newly_from_refinement},
                    )
                except Exception:
                    pass

    # S1-2 FIX: pad with symbol-scored fallback for any remaining budget.
    if len(selected) < max_files:
        remaining = [p for p in paths if p not in selected_set]
        scored_remaining = _sort_by_symbol_relevance(remaining, symbols)
        original_count = len(selected)
        for p in scored_remaining:
            selected.append(p)
            if len(selected) >= max_files:
                break
        padded_count = len(selected) - original_count
        if padded_count > 0:
            logger.info(
                "Symbol-scored padding: added %d file(s) to fill context budget.",
                padded_count,
            )
    else:
        padded_count = 0

    logger.info(
        "File ranking complete: %d total (%d LLM, %d import-graph, %d iterative, %d padded)",
        len(selected),
        len(llm_selected),
        len(newly_from_imports),
        len(newly_from_refinement) if 'newly_from_refinement' in locals() else 0,
        padded_count if 'padded_count' in locals() else 0,
    )
    return selected, llm_selected


def refresh_context_for_feedback(
    reviewer_feedback: list[str],
    context: "TicketContext",
    gitlab: Any,
    audit: "AuditLogger",
    checkpoint: Any = None,
) -> None:
    """
    Phase 2 Gate-2 context re-expansion.

    When a human reviewer references files or symbols not included in the
    initial Stage 1 context snapshot, this function expands the context
    by fetching those additional files from the VCS platform.

    Modifies ``context.source_files`` in-place.

    Fix 2 (ContextIssue): Uses ``code_branch`` from *checkpoint* (when
    available) instead of always falling back to ``default_branch``.
    This ensures newly-referenced files are fetched at their latest
    committed state on the PR branch, not the original master snapshot.
    """
    # ── Fix 2 (ContextIssue): Prefer code_branch over default_branch ───
    # If a checkpoint is provided and has a code_branch, fetch from there.
    # Fall back to context.default_branch when no branch has been created yet
    # (e.g. first-run before Stage 3 has started).
    target_ref = (
        getattr(checkpoint, "code_branch", None) or context.default_branch
    )
    logger.info(
        "Gate-2 context expansion: fetching new files from ref '%s' (code_branch=%s).",
        target_ref,
        getattr(checkpoint, "code_branch", None),
    )

    existing_paths = set(context.source_files.keys())
    expansion = find_additional_context_files(
        reviewer_feedback=reviewer_feedback,
        repo_root=".",  # No local disk access in CI -- use gitlab client.
        existing_context_paths=existing_paths,
        all_file_paths=context.all_file_paths,
        gitlab=gitlab,
        branch=target_ref,
        max_new_files=10,
    )

    newly_fetched: list[str] = []
    for path in expansion.new_files_found:
        if path not in context.source_files:
            try:
                content = gitlab.get_file_content(path, ref=target_ref)
                context.source_files[path] = content
                newly_fetched.append(path)
            except Exception as exc:
                logger.warning("Could not fetch context-expansion file %s: %s", path, exc)

    if newly_fetched:
        logger.info(
            "Gate-2 context expansion: added %d new file(s): %s",
            len(newly_fetched), newly_fetched,
        )
        audit.log(
            stage=1,
            event_type="context_expanded_for_review",
            payload={"new_files": newly_fetched, "symbols_found": len(expansion.search_hits), "ref": target_ref},
        )
    else:
        logger.info("Gate-2 context expansion: no new files needed.")

