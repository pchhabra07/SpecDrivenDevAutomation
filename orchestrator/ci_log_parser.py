"""
CI Log Parser -- Structured Error Extraction (Fix for Issue 2).

Converts raw CI/CD log output into structured ParsedError objects so that
Stage 3 gives the LLM precise file:line:message diagnostics instead of a
100-line raw log dump.

Supports pytest, Jest/Vitest, ESLint, and a generic fallback.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ParsedError:
    """A single structured error extracted from a CI log."""
    file: str
    line: int | None
    message: str
    test_name: str | None = None
    error_type: str = "error"  # "error" | "assertion" | "import" | "syntax"


def parse_ci_log(log_output: str) -> list[ParsedError]:
    """Parse raw CI log into structured errors. Tries pytest, then Jest, then generic."""
    errors: list[ParsedError] = []

    errors.extend(_parse_pytest(log_output))
    if not errors:
        errors.extend(_parse_jest(log_output))
    if not errors:
        errors.extend(_parse_generic(log_output))

    # Deduplicate by (file, line, message prefix)
    seen: set[tuple] = set()
    deduped: list[ParsedError] = []
    for e in errors:
        key = (e.file, e.line, e.message[:80])
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    return deduped[:20]  # Cap at 20 to avoid token explosion


def format_errors_for_prompt(errors: list[ParsedError]) -> str:
    """Format parsed errors as a structured LLM prompt section."""
    if not errors:
        return ""

    lines = [
        "## Structured Failure Analysis",
        "The following specific errors were extracted from CI logs.",
        "Fix EACH of these precisely. Do NOT make unrelated changes:",
        "",
    ]
    for i, err in enumerate(errors, 1):
        loc = err.file
        if err.line:
            loc += f":{err.line}"
        test = f" (test: `{err.test_name}`)" if err.test_name else ""
        lines.append(f"**Error {i}** [{err.error_type.upper()}] `{loc}`{test}")
        lines.append("```")
        lines.append(err.message.strip()[:400])
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ── pytest ────────────────────────────────────────────────────────────────

_PY_FAILED = re.compile(r"^FAILED\s+(.+?)\s+-\s+(.+)$", re.MULTILINE)
_PY_ERROR  = re.compile(r"^ERROR\s+(.+?)\s+-\s+(.+)$", re.MULTILINE)
_PY_LOC    = re.compile(r"([^\s]+\.py):(\d+):\s*(.*)")
_PY_IMPORT = re.compile(r"(ImportError|ModuleNotFoundError):\s*(.+)")
_PY_SYNTAX = re.compile(r"SyntaxError:\s*(.+)")


def _parse_pytest(log: str) -> list[ParsedError]:
    errors: list[ParsedError] = []

    for m in list(_PY_FAILED.finditer(log)) + list(_PY_ERROR.finditer(log)):
        test_path = m.group(1).strip()
        reason = m.group(2).strip()
        file_part = test_path.split("::")[0]
        test_name = "::".join(test_path.split("::")[1:]) or None
        errors.append(ParsedError(
            file=file_part.replace("\\", "/"),
            line=None,
            message=reason,
            test_name=test_name,
            error_type="assertion" if "AssertionError" in reason else "error",
        ))

    for m in _PY_LOC.finditer(log):
        fpath = m.group(1).strip().replace("\\", "/")
        if fpath.startswith("<"):
            continue
        lineno = int(m.group(2))
        msg = m.group(3).strip()
        if msg:
            errors.append(ParsedError(
                file=fpath, line=lineno, message=msg,
                error_type="syntax" if "SyntaxError" in msg else "error",
            ))

    for m in _PY_IMPORT.finditer(log):
        errors.append(ParsedError(
            file="<imports>", line=None,
            message=f"{m.group(1)}: {m.group(2).strip()}",
            error_type="import",
        ))

    return errors


# ── Jest / Vitest ─────────────────────────────────────────────────────────

_JEST_BLOCK  = re.compile(r"(?=\u25cf\s+)", re.UNICODE)
_JEST_TITLE  = re.compile(r"[\u25cf\u2022]\s+(.+?)(?:\n|$)")
_JEST_LOC    = re.compile(r"at\s+.+?\((.+?\.(?:ts|tsx|js|jsx)):(\d+):\d+\)")
_JEST_EXP    = re.compile(r"Expected(?:\s+value)?:\s+(.+?)(?:\n|$)")
_JEST_REC    = re.compile(r"Received(?:\s+value)?:\s+(.+?)(?:\n|$)")


def _parse_jest(log: str) -> list[ParsedError]:
    errors: list[ParsedError] = []

    # Split on failure block markers (● or •)
    sections = re.split(r"(?=[\u25cf\u2022]\s+)", log)
    for section in sections:
        if not section.strip():
            continue
        title_m = _JEST_TITLE.match(section.strip())
        test_name = title_m.group(1).strip() if title_m else None
        if not test_name:
            continue

        loc_m = _JEST_LOC.search(section)
        fpath = loc_m.group(1).replace("\\", "/") if loc_m else "<unknown>"
        lineno = int(loc_m.group(2)) if loc_m else None

        exp_m = _JEST_EXP.search(section)
        rec_m = _JEST_REC.search(section)
        if exp_m and rec_m:
            msg = f"Expected: {exp_m.group(1).strip()} | Received: {rec_m.group(1).strip()}"
        else:
            lines = [l for l in section.splitlines()[1:6]
                     if l.strip() and not l.strip().startswith("at ")]
            msg = lines[0].strip() if lines else section[:200]

        errors.append(ParsedError(
            file=fpath, line=lineno, message=msg,
            test_name=test_name, error_type="assertion",
        ))

    return errors


# ── Generic fallback ──────────────────────────────────────────────────────

_GENERIC = re.compile(
    r"(?:ERROR|FAIL|error|Error)\s+(?:in\s+)?"
    r"([^\s]+\.(?:py|js|ts|tsx|jsx|java|go|rs))(?::(\d+))?\s*[:\-]?\s*(.+)",
    re.MULTILINE,
)


def _parse_generic(log: str) -> list[ParsedError]:
    return [
        ParsedError(
            file=m.group(1).strip().replace("\\", "/"),
            line=int(m.group(2)) if m.group(2) else None,
            message=m.group(3).strip()[:300],
            error_type="error",
        )
        for m in _GENERIC.finditer(log)
    ]
