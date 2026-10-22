from __future__ import annotations
import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_IGNORE_DIRS = frozenset(["node_modules", "dist", "build", ".git", "coverage", "__pycache__", ".venv"])

_SYMBOL_PATTERNS = [
    re.compile(r"\.([a-zA-Z][\w-]{2,})"),
    re.compile(r"#([a-zA-Z][\w-]{2,})"),
    re.compile(r"def\s+([a-zA-Z_][\w]{2,})"),
    re.compile(r"function\s+([a-zA-Z_][\w]{2,})"),
    re.compile(r"const\s+([A-Z][\w]{2,})"),
    re.compile(r"class\s+([A-Z][\w]{2,})"),
    re.compile(r"import.*?[{\s]([A-Z][\w]{2,})"),
]

_SOURCE_GLOBS = (
    "*.py", "*.js", "*.ts", "*.tsx", "*.jsx",
    "*.html", "*.htm", "*.css", "*.scss", "*.sass",
    # S1-6: expanded to cover non-JS/TS source files
    "*.java", "*.kt", "*.go", "*.rs", "*.cs", "*.rb",
    "*.cpp", "*.c", "*.h", "*.swift", "*.scala",
    "*.php", "*.sh",
    "*.json", "*.yaml", "*.yml",
)


_KNOWN_EXTENSIONS = tuple(g.lstrip("*") for g in _SOURCE_GLOBS)


@dataclass
class SearchHit:
    path: str
    line_number: int
    line_content: str
    symbol: str


@dataclass
class ContextExpansionResult:
    new_files_found: list = field(default_factory=list)
    search_hits: list = field(default_factory=list)


def extract_symbols_from_text(text: str) -> list:
    symbols: set = set()
    for token in text.split():
        token = token.strip(".,;:()[]'\"`")
        if ("/" in token or "\\" in token) and any(token.lower().endswith(ext) for ext in _KNOWN_EXTENSIONS):
            symbols.add(token.replace("\\", "/"))
    for pattern in _SYMBOL_PATTERNS:
        for m in pattern.finditer(text):
            sym = m.group(1)
            if len(sym) >= 3:
                symbols.add(sym)
    return list(symbols)


def search_repository_for_symbols(
    repo_root,
    symbols: list,
    all_file_paths=None,
    max_results_per_symbol: int = 5,
    gitlab=None,
    branch: str = "main",
    max_api_scans: int = 60,
) -> list:
    """
    Search source files for symbol occurrences.

    S1-3 FIX: In CI, no local clone exists, so the original disk-read path
    silently returned an empty list for every query, making Gate-2 context
    expansion a no-op.  When a ``gitlab`` client is supplied the function
    now fetches file content via the VCS API instead of reading from disk.
    The disk-read fallback is preserved for local-dev / test environments.

    Logical protections:
    - Early loop termination as soon as all symbols have found max results.
    - Path scoring to prioritize files matching searched symbol names.
    - Capped API scans (default 60) to protect against rate limits & latency.
    """
    root = Path(repo_root)
    hits = []

    # Determine the candidate file list.
    if all_file_paths:
        candidate_paths = [
            p for p in all_file_paths
            if _is_searchable_file(p) and not _in_ignored_dir(p)
        ]
        if gitlab:
            symbols_lower = [s.lower() for s in symbols]

            def _score_path(p: str) -> int:
                p_lower = p.lower()
                return sum(1 for s in symbols_lower if s in p_lower)

            candidate_paths = sorted(candidate_paths, key=_score_path, reverse=True)[:max_api_scans]
    elif gitlab:
        # CI mode: no all_file_paths given and no local clone — skip.
        logger.warning(
            "No all_file_paths provided with gitlab client; symbol search skipped."
        )
        return []
    else:
        # Local-dev / test mode: walk the disk.
        candidate_paths = [
            str(f.relative_to(root)).replace("\\", "/")
            for f in _walk_source_files(root)
        ]

    sym_counts = {s: 0 for s in symbols}

    for rel_path in candidate_paths:
        # Early break if all symbols have satisfied max_results_per_symbol
        if symbols and all(sym_counts.get(s, 0) >= max_results_per_symbol for s in symbols):
            break

        # Read content — prefer the VCS API when available.
        text = None
        if gitlab:
            try:
                text = gitlab.get_file_content(rel_path, ref=branch)
            except Exception:
                continue
        else:
            abs_path = root / rel_path
            if not abs_path.is_file():
                continue
            try:
                text = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

        if not text:
            continue

        for sym in symbols:
            if sym_counts.get(sym, 0) >= max_results_per_symbol:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if sym in line:
                    hits.append(SearchHit(
                        path=rel_path, line_number=lineno,
                        line_content=line.rstrip(), symbol=sym,
                    ))
                    sym_counts[sym] = sym_counts.get(sym, 0) + 1
                    break
    return hits


def find_additional_context_files(
    reviewer_feedback: list,
    repo_root,
    existing_context_paths: set,
    all_file_paths=None,
    gitlab=None,
    branch: str = "main",
    max_new_files: int = 10,
):
    combined_text = " ".join(reviewer_feedback)
    symbols = extract_symbols_from_text(combined_text)
    logger.info("Extracted %d symbols from reviewer feedback: %s", len(symbols), symbols[:20])
    root = Path(repo_root) if repo_root else None
    expansion = ContextExpansionResult()

    for sym in list(symbols):
        if "/" in sym and sym not in existing_context_paths:
            if gitlab:
                try:
                    gitlab.get_file_content(sym, ref=branch)
                    expansion.new_files_found.append(sym)
                    symbols.remove(sym)
                except Exception:
                    pass
            elif root and (root / sym).is_file():
                expansion.new_files_found.append(sym)
                symbols.remove(sym)

    if symbols and (root or all_file_paths):
        # S1-3 FIX: propagate gitlab + branch so CI-mode search actually works.
        hits = search_repository_for_symbols(
            repo_root=root or Path("."),
            symbols=symbols,
            all_file_paths=all_file_paths,
            gitlab=gitlab,
            branch=branch,
        )
        expansion.search_hits = hits
        for hit in hits:
            if hit.path not in existing_context_paths and hit.path not in expansion.new_files_found:
                expansion.new_files_found.append(hit.path)

    expansion.new_files_found = expansion.new_files_found[:max_new_files]
    logger.info("Context expansion found %d new file(s): %s",
                len(expansion.new_files_found), expansion.new_files_found)
    return expansion


def _is_searchable_file(path: str) -> bool:
    basename = Path(path).name
    return any(fnmatch.fnmatch(basename, g) for g in _SOURCE_GLOBS)


def _in_ignored_dir(path: str) -> bool:
    parts = Path(path).parts
    return any(part in _IGNORE_DIRS for part in parts)


def _walk_source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if _is_searchable_file(fname):
                yield fpath
