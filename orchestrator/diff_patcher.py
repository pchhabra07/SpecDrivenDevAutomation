from __future__ import annotations
import re
from dataclasses import dataclass, field

_SM = '<<<<<<< SEARCH'
_SEP = '======='
_RM = '>>>>>>> REPLACE'
# Issue 10 fix: \S+ stops path at whitespace, preventing annotation text from being captured.
_FILE_RE = re.compile(
    r"^\s*(?://\s*FILE:|#\s*FILE:|/\*\s*FILE:)\s*(\S+)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class PatchResult:
    path: str
    original_content: str
    patched_content: str
    blocks_applied: int = 0
    blocks_failed: int = 0
    errors: list = field(default_factory=list)
    is_new_file: bool = False

    @property
    def success(self) -> bool:
        return self.blocks_failed == 0 and not self.errors


class SRBlock:
    __slots__ = ("search", "replace")
    def __init__(self, search: str, replace: str):
        self.search = search
        self.replace = replace


def parse_llm_response(llm_output: str) -> dict:
    result = {}
    for path, sec in _split_sections(llm_output).items():
        blocks = _extract_blocks(sec)
        if blocks:
            result[path] = blocks
        else:
            cleaned = _strip_fences(sec).strip()
            if cleaned:
                result[path] = cleaned
    return result


def apply_patches(path: str, existing_content, patches):
    original = existing_content or ""
    if isinstance(patches, str):
        return PatchResult(path=path, original_content=original,
                           patched_content=patches, blocks_applied=1, is_new_file=True)
    res = PatchResult(path=path, original_content=original, patched_content=original)
    current = original
    for blk in patches:
        patched, err = _apply_block(current, blk)
        if err:
            res.blocks_failed += 1
            res.errors.append(err)
        else:
            res.blocks_applied += 1
            current = patched
    res.patched_content = current
    return res


def apply_patches_to_sources(
    parsed: dict,
    current_sources: dict,
) -> tuple[dict, list, bool]:
    """
    Apply parsed patches to source files.

    Returns
    -------
    tuple[dict, list, bool]
        - updated: {path: patched_content} for every SUCCESS.
        - results: full list of PatchResult objects.
        - all_succeeded: True only when every patch in every file applied cleanly.
          Callers should use this as an all-or-nothing gate before committing.
    """
    updated: dict = {}
    results: list = []
    all_succeeded = True
    for path, patches in parsed.items():
        pr = apply_patches(path, current_sources.get(path), patches)
        results.append(pr)
        if pr.success:
            updated[path] = pr.patched_content
        else:
            all_succeeded = False
    return updated, results, all_succeeded


def _split_sections(text: str) -> dict:
    sections = {}
    matches = list(_FILE_RE.finditer(text))
    for i, m in enumerate(matches):
        path = m.group(1).strip().strip("'\"").replace("\\\\", "/")
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        sections[path] = text[start:end]
    return sections


def _extract_blocks(text: str) -> list:
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == _SM:
            sl = []
            i += 1
            while i < len(lines) and lines[i].strip() != _SEP:
                sl.append(lines[i])
                i += 1
            rl = []
            i += 1
            while i < len(lines) and lines[i].strip() != _RM:
                rl.append(lines[i])
                i += 1
            blocks.append(SRBlock(search="\n".join(sl), replace="\n".join(rl)))
        i += 1
    return blocks


def _apply_block(content: str, blk):
    st = blk.search
    cnt = content.count(st)
    if cnt == 1:
        return content.replace(st, blk.replace, 1), None
    if cnt > 1:
        return content, f"AMBIGUOUS: {cnt} occurrences.\nSEARCH: {st[:120]!r}"
    patched, ok = _fuzzy_replace(content, st, blk.replace)
    if ok:
        return patched, None
    return content, f"NOT FOUND.\nSEARCH: {st[:200]!r}"


def _fuzzy_replace(content: str, search: str, replace: str):
    nc = [" ".join(l.split()) for l in content.splitlines()]
    ns = [" ".join(l.split()) for l in search.splitlines()]
    if not ns:
        return content, False
    n = len(ns)
    for i in range(len(nc) - n + 1):
        if nc[i:i+n] == ns:
            cl = content.splitlines()
            out = cl[:i] + replace.splitlines() + cl[i+n:]
            return "\n".join(out), True
    return content, False


def _strip_fences(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
