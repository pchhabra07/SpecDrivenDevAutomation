"""
SHA-256 tamper-check for locked test files.

After every code-generation iteration the orchestrator calls
``check_tampering()`` to verify that no locked test file has been
modified.  If any file's hash no longer matches the stored value the
coding loop is halted immediately.

Implements FR-10, FR-11.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def hash_file(path: str | Path) -> str:
    """Return the hex-encoded SHA-256 digest of a file's contents."""
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def hash_content(content: str | bytes) -> str:
    """Return the hex-encoded SHA-256 digest of in-memory content."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def build_lock_hashes(file_contents: dict[str, str]) -> dict[str, str]:
    """
    Build the initial lock-hash mapping from a dict of
    ``{relative_path: file_content_string}``.

    Called once at the end of Stage 2 after the human approves the tests.
    """
    return {path: hash_content(content) for path, content in file_contents.items()}


def check_tampering(
    locked_hashes: dict[str, str],
    current_contents: dict[str, str],
) -> list[str]:
    """
    Compare current test-file contents against their locked SHA-256 hashes.

    Parameters
    ----------
    locked_hashes:
        ``{relative_path: sha256_hex}`` produced by ``build_lock_hashes``.
    current_contents:
        ``{relative_path: current_content_string}`` for the same set of paths.

    Returns
    -------
    list[str]
        Relative paths of files whose content no longer matches their
        locked hash.  Empty list means no tampering detected.
    """
    tampered: list[str] = []
    for path, expected_hash in locked_hashes.items():
        current = current_contents.get(path)
        if current is None:
            # File was deleted — treat as tampered.
            logger.warning("Locked test file MISSING: %s", path)
            tampered.append(path)
            continue
        actual_hash = hash_content(current)
        if actual_hash != expected_hash:
            logger.warning(
                "TAMPER DETECTED: %s (expected=%s, actual=%s)",
                path,
                expected_hash[:12],
                actual_hash[:12],
            )
            tampered.append(path)
    return tampered
