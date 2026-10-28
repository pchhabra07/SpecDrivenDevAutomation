from __future__ import annotations
import fnmatch
import logging
import math
import os
import re
from collections import Counter
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


# ---------------------------------------------------------------------------
# BM25 — term-frequency / document-frequency ranking (semantic-style)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lower-case alphanumeric tokeniser shared by BM25 helpers."""
    return re.findall(r"[a-z0-9_]{2,}", text.lower())


def rank_by_bm25(
    query: str,
    documents: dict[str, str],
    k1: float = 1.5,
    b: float = 0.75,
    top_n: int = 60,
) -> list[str]:
    """
    Rank *documents* (path → text) by BM25 relevance to *query*.

    BM25 is a probabilistic TF-IDF variant that accounts for document length
    normalisation and term saturation.  It captures meaning much better than
    plain symbol-in-path scoring because it reads actual file *content* and
    weights terms by inverse document frequency (IDF).

    This is the lightweight equivalent of Antigravity's embedding-based
    semantic search — no external API or model required.

    Parameters
    ----------
    query : str
        Combined ticket text (summary + description + acceptance criteria).
    documents : dict[str, str]
        ``{file_path: snippet_or_full_content}`` to score.
    k1, b : float
        Standard BM25 hyper-parameters (Okapi BM25 defaults).
    top_n : int
        Return at most *top_n* paths sorted highest-score first.

    Returns
    -------
    list[str]
        Paths from *documents* ordered by descending BM25 score.
    """
    if not documents:
        return []

    query_terms = _tokenize(query)
    if not query_terms:
        return list(documents.keys())[:top_n]

    # Tokenise every document once.
    tokenised: dict[str, list[str]] = {
        path: _tokenize(content)
        for path, content in documents.items()
    }

    # Average document length
    doc_lengths = {path: len(toks) for path, toks in tokenised.items()}
    avg_dl = sum(doc_lengths.values()) / max(len(doc_lengths), 1)
    N = len(documents)

    # Document-frequency per query term
    df: Counter[str] = Counter()
    for toks in tokenised.values():
        tok_set = set(toks)
        for term in query_terms:
            if term in tok_set:
                df[term] += 1

    # Score each document
    scores: dict[str, float] = {}
    for path, toks in tokenised.items():
        tf_map: Counter[str] = Counter(toks)
        dl = doc_lengths[path]
        score = 0.0
        for term in query_terms:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            idf = math.log((N - df[term] + 0.5) / (df[term] + 0.5) + 1.0)
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
            score += idf * tf_norm
        scores[path] = score

    ranked = sorted(scores.keys(), key=lambda p: scores[p], reverse=True)
    return ranked[:top_n]


# ---------------------------------------------------------------------------
# Import / call-graph tracer
# ---------------------------------------------------------------------------

# Language-specific import patterns.  Each yields (module_or_path,).
_IMPORT_PATTERNS: list[re.Pattern[str]] = [
    # Python:  from .module import X  |  import module
    re.compile(r"^(?:from\s+([\w./]+)\s+import|import\s+([\w./]+))", re.MULTILINE),
    # JS/TS:   import ... from './path'  |  require('./path')
    re.compile(r"""(?:from|require)\s*\(?['"](\.{1,2}/[^'"]+)['"]\)?"""),
    # Go:      import "pkg/path"
    re.compile(r'''import\s+["']([\w./]+)["']'''),
    # Java/Kotlin:  import com.example.Foo
    re.compile(r"^import\s+([\w.]+);", re.MULTILINE),
    # Rust:    use crate::module::Thing
    re.compile(r"^use\s+([\w:]+)", re.MULTILINE),
]


def _resolve_import_to_path(
    raw: str,
    current_file: str,
    all_file_paths: list[str],
) -> str | None:
    """
    Try to map a raw import token (e.g. ``./utils``, ``com.example.Foo``,
    ``crate::auth::handler``) to a concrete path in *all_file_paths*.

    Returns the matched path or *None* if not resolvable.
    """
    raw = raw.strip()
    if not raw:
        return None

    # Normalise separator
    raw_norm = raw.replace(".", "/").replace("::", "/").replace("\\", "/")

    # Resolve JS/TS relative imports against the current file's directory
    if raw.startswith("./") or raw.startswith("../"):
        base_dir = "/".join(current_file.split("/")[:-1])
        relative = raw.lstrip("./")
        candidate_prefix = f"{base_dir}/{relative}".lstrip("/")
    else:
        candidate_prefix = raw_norm.lstrip("/")

    # Remove leading dots from Python relative imports
    candidate_prefix = candidate_prefix.lstrip("./")

    # Extension inference — try with common extensions
    extensions = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".kt",
                  ".rs", ".cs", ".rb", ".cpp", ".c", ".swift")

    all_lower = {p.replace("\\", "/"): p for p in all_file_paths}

    # Exact match or suffix match
    for ext in ("", *extensions):
        candidate = (candidate_prefix + ext).lower()
        for norm_path, orig_path in all_lower.items():
            if norm_path == candidate or norm_path.endswith("/" + candidate):
                return orig_path

    return None


def trace_import_graph(
    selected_files: dict[str, str],
    all_file_paths: list[str],
    max_depth: int = 1,
    max_new_files: int = 20,
) -> list[str]:
    """
    Starting from *selected_files*, parse import / use / require statements
    and return additional file paths that are imported but not yet in the
    selected set.

    This mirrors Antigravity's call-graph tracing: after the LLM picks the
    most relevant files, we follow their import edges one level deep to
    automatically include modules they depend on.

    Parameters
    ----------
    selected_files : dict[str, str]
        Already-selected files ``{path: content}``.
    all_file_paths : list[str]
        Full repository file tree (paths only).
    max_depth : int
        How many import hops to follow (1 = direct imports only).
    max_new_files : int
        Cap on additional files returned.

    Returns
    -------
    list[str]
        Paths of newly discovered dependency files (not in *selected_files*).
    """
    existing = set(selected_files.keys())
    frontier: dict[str, str] = dict(selected_files)
    discovered: list[str] = []

    for _depth in range(max_depth):
        next_frontier: dict[str, str] = {}
        for file_path, content in frontier.items():
            raw_imports: set[str] = set()
            for pattern in _IMPORT_PATTERNS:
                for match in pattern.finditer(content):
                    # Some patterns have multiple groups; take the first non-empty
                    raw = next((g for g in match.groups() if g), match.group(0))
                    if raw:
                        raw_imports.add(raw.strip())

            for raw in raw_imports:
                resolved = _resolve_import_to_path(raw, file_path, all_file_paths)
                if resolved and resolved not in existing and resolved not in next_frontier:
                    next_frontier[resolved] = ""  # content fetched by caller
                    discovered.append(resolved)
                    if len(discovered) >= max_new_files:
                        break
            if len(discovered) >= max_new_files:
                break

        if not next_frontier or len(discovered) >= max_new_files:
            break
        frontier = next_frontier
        existing |= set(next_frontier.keys())

    logger.info(
        "Import graph trace: found %d new dependency file(s) from %d selected file(s).",
        len(discovered), len(selected_files),
    )
    return discovered[:max_new_files]


# ---------------------------------------------------------------------------
# Identifier extractor — for iterative context refinement second pass
# ---------------------------------------------------------------------------

_IDENTIFIER_PATTERNS: list[re.Pattern[str]] = [
    # Python functions/classes
    re.compile(r"^(?:def|class|async def)\s+([A-Za-z_][A-Za-z0-9_]{2,})", re.MULTILINE),
    # JS/TS functions/classes/components
    re.compile(r"(?:function|class|const|let|var)\s+([A-Z][A-Za-z0-9]{2,})"),
    re.compile(r"export\s+(?:default\s+)?(?:function|class|const)\s+([A-Za-z_][A-Za-z0-9_]{2,})"),
    # React components (PascalCase identifiers in JSX/TSX)
    re.compile(r"<([A-Z][A-Za-z0-9]{2,})"),
    # Go functions
    re.compile(r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]{2,})", re.MULTILINE),
    # Java/Kotlin classes
    re.compile(r"^(?:public|private|protected)?\s+(?:class|interface|enum)\s+([A-Z][A-Za-z0-9]{2,})", re.MULTILINE),
]


def extract_identifiers_from_content(content: str, max_identifiers: int = 30) -> list[str]:
    """
    Extract prominent function, class, and component names from *content*.

    Used in the iterative context refinement pass: after the LLM selects the
    top files, we extract their exported identifiers and search the *rest* of
    the repository for files that reference those identifiers — surfacing
    callers / consumers that may also need changes.

    Returns
    -------
    list[str]
        De-duplicated identifier names, most frequent first.
    """
    found: Counter[str] = Counter()
    for pattern in _IDENTIFIER_PATTERNS:
        for match in pattern.finditer(content):
            name = match.group(1)
            if len(name) >= 3:
                found[name] += 1

    # Return sorted by frequency (most-defined = most central)
    return [name for name, _ in found.most_common(max_identifiers)]

