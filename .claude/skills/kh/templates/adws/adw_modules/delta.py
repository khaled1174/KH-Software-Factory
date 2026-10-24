"""Controller-computed task delta: authoritative diff and verification files.

The reviewer receives what the controller measured, not what the builder
reported. Nothing here judges the change; it makes the change inspectable.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path

VERIFICATION_PARTS = {"test", "tests", "spec", "specs", "__tests__", "e2e", "fixtures"}
VERIFICATION_NAMES = re.compile(r"^(test_.*|.*_test\.[^.]+|.*\.test\.[^.]+|.*\.spec\.[^.]+|conftest\.py"
                                r"|pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|package\.json"
                                r"|jest\.config\..*|vitest\.config\..*|tsconfig.*\.json|\.eslintrc.*"
                                r"|eslint\.config\..*|ruff\.toml|mypy\.ini|justfile|Makefile)$", re.IGNORECASE)


def verification_paths(paths: list[str], cfg=None) -> list[str]:
    """Changed files that define or configure the checks the task relies on."""
    argv_tokens = set()
    for argv in getattr(getattr(cfg, "defaults", None), "quality_checks", {}).values() or []:
        argv_tokens.update(str(a).lstrip("./") for a in argv)
    result = []
    for path in paths:
        parts = Path(path).parts
        if (any(part.lower() in VERIFICATION_PARTS for part in parts[:-1])
                or VERIFICATION_NAMES.match(parts[-1]) or path in argv_tokens):
            result.append(path)
    return result


def _text(entry) -> list[str] | None:
    """Lines with a guaranteed trailing newline; None for binary content."""
    if entry is None:
        return []
    if entry.kind != "file":
        return None
    try:
        text = entry.data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        # Keep unified-diff lines from running together ("-a+b") and record the
        # missing terminator the way git does.
        lines[-1] += "\n" + NO_NEWLINE
    return lines


NO_NEWLINE = "\\ No newline at end of file\n"


def _describe(entry) -> str:
    if entry is None:
        return "absent"
    if entry.kind == "symlink":
        return f"symlink -> {entry.data.decode('utf-8', 'replace')} (mode {entry.mode:04o})"
    if entry.kind == "directory":
        return f"directory (mode {entry.mode:04o})"
    return (f"file {len(entry.data)} bytes sha256 {hashlib.sha256(entry.data).hexdigest()[:16]} "
            f"(mode {entry.mode:04o})")


def unified_diff(baseline, current, paths: list[str]) -> str:
    """Unified diff of the exact controller-observed changes for paths.

    Every changed path gets a header. Text content is diffed line by line;
    mode changes, symlink target changes, kind changes and binary content are
    stated explicitly with before/after facts instead of being silent.
    """
    chunks = []
    for path in paths:
        before, after = baseline.entries.get(path), current.entries.get(path)
        block = [f"diff --kh a/{path} b/{path}\n"]
        if before is not None and after is not None and before.mode != after.mode:
            block.append(f"old mode {before.mode:04o}\nnew mode {after.mode:04o}\n")
        if (before is not None and after is not None and before.kind != after.kind) or \
           (before or after).kind in ("symlink", "directory"):
            block.append(f"before: {_describe(before)}\nafter:  {_describe(after)}\n")
            chunks.append("".join(block))
            continue
        old, new = _text(before), _text(after)
        if old is None or new is None:
            block.append(f"binary change\nbefore: {_describe(before)}\nafter:  {_describe(after)}\n")
            chunks.append("".join(block))
            continue
        if old == new:
            block.append("(content identical; see mode lines above)\n" if len(block) > 1
                         else "(no content difference recorded)\n")
            chunks.append("".join(block))
            continue
        header_a = f"a/{path}" if before is not None else "/dev/null"
        header_b = f"b/{path}" if after is not None else "/dev/null"
        block.extend(difflib.unified_diff(old, new, header_a, header_b, n=3))
        chunks.append("".join(block))
    return "".join(chunks) if chunks else "(no source changes)\n"
