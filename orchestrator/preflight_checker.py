from __future__ import annotations
import ast
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import shutil
import subprocess

logger = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    path: str
    passed: bool
    errors: list = field(default_factory=list)
    language: str = ""
    confidence: str = "high"

    @property
    def error_summary(self) -> str:
        return "; ".join(self.errors) if self.errors else "OK"


def check_all(files: dict) -> tuple:
    results = []
    for path, content in files.items():
        results.append(_check_file(path, content))
    all_passed = all(r.passed for r in results)
    return all_passed, results


def build_repair_prompt(results: list) -> str:
    failed = [r for r in results if not r.passed]
    lines = [
        "Your previous code changes contain syntax errors caught by pre-flight verification.",
        "Fix ALL of the following errors. Only fix these syntax errors, do not change other logic.",
        "",
    ]
    for r in failed:
        conf_note = " (heuristic check)" if r.confidence == "low" else ""
        lines.append(f"FILE: {r.path} ({r.language}){conf_note}")
        for err in r.errors:
            lines.append(f"  ERROR: {err}")
        lines.append("")
    lines.append("Return corrected files using the same SEARCH/REPLACE block format.")
    return "\n".join(lines)


def _check_file(path: str, content: str) -> PreflightResult:
    ext = Path(path).suffix.lower().lstrip(".")
    if ext == "py":
        return _check_python(path, content)
    if ext in ("js", "ts", "jsx", "tsx", "mjs", "cjs"):
        return _check_js_ts(path, content)
    if ext in ("html", "htm"):
        return _check_html(path, content)
    if ext in ("css", "scss", "sass"):
        return _check_css(path, content)
    if ext == "json":
        return _check_json(path, content)
    if ext in ("yaml", "yml"):
        return _check_yaml(path, content)
    return PreflightResult(path=path, passed=True, language=ext or "unknown")


def _check_python(path: str, content: str) -> PreflightResult:
    try:
        ast.parse(content, filename=path)
        return PreflightResult(path=path, passed=True, language="python")
    except SyntaxError as exc:
        return PreflightResult(path=path, passed=False, language="python",
            errors=[f"SyntaxError on line {exc.lineno}: {exc.msg}"])


def _looks_like_jsx(content: str) -> bool:
    """Rough heuristic to detect JSX elements in JS files."""
    return bool(re.search(r"<\s*([A-Z]\w+|div|span|p|button|a|input|form|table|ul|li)\b[^>]*>", content))


def _run_node_syntax_check(content: str, is_module: bool = False) -> tuple[bool, str] | None:
    """
    Attempt real JavaScript syntax check using `node --check`.
    Returns (passed, error_message) or None if Node is unavailable/timed out.
    """
    if not shutil.which("node"):
        return None
    cmd = ["node", "--check"]
    if is_module:
        cmd.append("--input-type=module")
    try:
        proc = subprocess.run(
            cmd,
            input=content,
            text=True,
            capture_output=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return True, ""
        err = proc.stderr.strip()
        if not is_module and ("Cannot use import statement" in err or "Unexpected token 'export'" in err):
            return _run_node_syntax_check(content, is_module=True)
        err_lines = [l for l in err.splitlines() if l.strip() and not l.strip().startswith("at ")][:4]
        return False, "\n".join(err_lines) or "Node syntax check failed"
    except Exception as exc:
        logger.debug("Node syntax check skipped: %s", exc)
        return None


def _check_js_ts(path: str, content: str) -> PreflightResult:
    ext = Path(path).suffix.lower().lstrip(".")
    is_pure_js = ext in ("js", "mjs", "cjs") and not _looks_like_jsx(content)

    # Try Node.js syntax check for pure JS
    if is_pure_js:
        is_mod = (ext == "mjs") or bool(re.search(r"^\s*(import|export)\b", content, re.MULTILINE))
        node_res = _run_node_syntax_check(content, is_module=is_mod)
        if node_res is not None:
            passed, err_msg = node_res
            if not passed:
                return PreflightResult(path=path, passed=False, language=ext, errors=[err_msg], confidence="high")
            return PreflightResult(path=path, passed=True, language=ext, confidence="high")

    # Fallback: heuristic bracket and backtick counting (confidence="low")
    errors = []
    cleaned = _strip_js_comments(content)
    for oc, cc in [("{", "}"), ("(", ")"), ("[", "]")]:
        opens = cleaned.count(oc)
        closes = cleaned.count(cc)
        if opens != closes:
            errors.append(f"Unbalanced: {opens}x '{oc}' vs {closes}x '{cc}'")
    bt = cleaned.count("`")
    if bt % 2 != 0:
        errors.append(f"Odd backtick count ({bt}): possible unclosed template literal")

    return PreflightResult(path=path, passed=not errors, language=ext, errors=errors, confidence="low")


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
    text = re.sub(r"`(?:[^`\\]|\\.)*`", "``", text, flags=re.DOTALL)
    return text


_VOID = frozenset(["area","base","br","col","embed","hr","img","input",
                   "link","meta","param","source","track","wbr"])


def _check_html(path: str, content: str) -> PreflightResult:
    errors = []
    tag_re = re.compile(r"<(/?)(\w[\w:-]*)(?:\s[^>]*)?>", re.I)
    stack = []
    for m in tag_re.finditer(content):
        closing = bool(m.group(1))
        tag = m.group(2).lower()
        if tag in _VOID:
            continue
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
            else:
                exp = f" (expected '</{stack[-1]}>')" if stack else ""
                errors.append(f"Unexpected closing tag '</{tag}>'{exp}")
        else:
            stack.append(tag)
    for unclosed in stack[-5:]:
        errors.append(f"Unclosed HTML tag: <{unclosed}>")
    return PreflightResult(path=path, passed=not errors, language="html", errors=errors)


def _check_css(path: str, content: str) -> PreflightResult:
    errors = []
    cleaned = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    cleaned = re.sub(r'"[^"]*"', '""', cleaned)
    cleaned = re.sub(r"'[^']*'", "''", cleaned)
    opens = cleaned.count("{")
    closes = cleaned.count("}")
    if opens != closes:
        errors.append(f"Unbalanced CSS braces: {opens}x '{{' vs {closes}x '}}'")
    ext = Path(path).suffix.lower().lstrip(".")
    return PreflightResult(path=path, passed=not errors, language=ext, errors=errors)


def _check_json(path: str, content: str) -> PreflightResult:
    try:
        json.loads(content)
        return PreflightResult(path=path, passed=True, language="json")
    except json.JSONDecodeError as exc:
        return PreflightResult(path=path, passed=False, language="json",
            errors=[f"JSONDecodeError at line {exc.lineno} col {exc.colno}: {exc.msg}"])


def _check_yaml(path: str, content: str) -> PreflightResult:
    try:
        import yaml
        yaml.safe_load(content)
        return PreflightResult(path=path, passed=True, language="yaml")
    except Exception as exc:
        return PreflightResult(path=path, passed=False, language="yaml",
            errors=[f"YAML error: {exc}"])
