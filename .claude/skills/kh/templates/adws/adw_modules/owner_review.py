"""Deterministic detection of changes the owner should know about.

The controller-measured delta is matched against categories the owner cares
about: money, auth, destructive schema statements, or more changed lines than
`max_changed_lines` (`size`). Matching reads changed paths and the *added* lines
of the exact snapshots the reviewer judges, never the builder's report.

Default `report` mode never stops the run: every hit becomes a requirement the
independent reviewer must rule on before approval, then the task promotes and
closes itself and the hits are listed in its receipt and task report for the
owner to read after completion. Opt-in `hold` mode stops promotion instead,
unless the accepted intent approves the category (`Approved sensitive
changes`); the held candidate's controller diff stays in the evidence.
"""
from __future__ import annotations

import difflib

from . import delta, permissions

SIZE = "size"


def known_categories(settings) -> set[str]:
    return set(settings.categories) | {SIZE}


def unknown_approvals(settings, approved) -> list[str]:
    """Approved names that match no configured category (typos fail closed)."""
    return sorted(set(approved) - known_categories(settings))


def _lines(entry):
    """Text lines of a snapshot entry; None for symlinks, directories and NUL-bearing binaries.

    Text that is not UTF-8 (cp1252, latin-1 SQL dumps) is still read, byte for
    byte as latin-1, so an encoding cannot hide a keyword or a large change.
    """
    if entry is None:
        return []
    if entry.kind != "file":
        return None
    lines = delta._text(entry)
    if lines is None and b"\x00" not in entry.data:
        lines = entry.data.decode("latin-1").splitlines(keepends=True)
    return lines


def _match(path, pattern) -> bool:
    """Case-insensitive project glob; `dir/` with a wildcard means everything under it."""
    path, pattern = path.lower(), pattern.lower()
    if pattern.endswith("/") and any(c in pattern for c in "*?"):
        pattern += "**"
    return permissions._matches(path, pattern)


def changed(baseline, current, paths) -> dict[str, tuple[list[str], int]]:
    """Per path: (added text lines, removed line count) between two snapshots.

    Symlink, directory and binary (NUL-bearing) changes contribute no lines;
    their paths still take part in path matching.
    """
    result = {}
    for path in paths:
        old = _lines(baseline.entries.get(path))
        new = _lines(current.entries.get(path))
        if old is None or new is None:
            result[path] = ([], 0)
            continue
        added, removed = [], 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
            if tag in ("replace", "delete"):
                removed += i2 - i1
            if tag in ("replace", "insert"):
                added.extend(new[j1:j2])
        result[path] = (added, removed)
    return result


def _first_match(rule, changes) -> str | None:
    for path in sorted(changes):
        for pattern in rule.paths:
            if _match(path, pattern):
                return f"path {path} matches {pattern}"
    for path in sorted(changes):
        for line in changes[path][0]:
            text = line.lower()
            for keyword in rule.keywords:
                if keyword in text:
                    return f'added line in {path} contains "{keyword}"'
    return None


def evaluate(settings, changes, approved=()) -> list[dict]:
    """Categories the owner still has to decide; an empty list may promote."""
    if not settings.enabled:
        return []
    approved = set(approved)
    holds = []
    for name in sorted(settings.categories):
        if name in approved:
            continue
        why = _first_match(settings.categories[name], changes)
        if why:
            holds.append({"category": name, "why": why})
    if settings.max_changed_lines and SIZE not in approved:
        counted = sum(len(added) + removed for path, (added, removed) in changes.items()
                      if not any(_match(path, g) for g in settings.size_exclude))
        if counted > settings.max_changed_lines:
            holds.append({"category": SIZE,
                          "why": f"{counted} changed lines > max_changed_lines {settings.max_changed_lines}"})
    return holds


def hold_message(holds, diff_path) -> str:
    reasons = "; ".join(f"{h['category']}: {h['why']}" for h in holds)
    names = ", ".join(h["category"] for h in holds)
    return (f"OWNER_REVIEW_REQUIRED: {reasons}. Nothing was promoted. Review the candidate diff "
            f"{diff_path}; to let this task promote it, add {names} to \"Approved sensitive changes\" "
            f"in the task's intent, re-accept it, and run the task again.")


ATTENTION = "Owner attention"


def requirement(flag) -> str:
    return (f"{ATTENTION} ({flag['category']}): {flag['why']} — the change is required by the task and is "
            f"safe to land without the owner present; it is reported to the owner after completion")


def replace_requirements(requirements, flags):
    """Flags describe the current candidate: earlier owner-attention requirements are replaced."""
    kept = [r for r in requirements if not r.startswith(ATTENTION + " (")]
    return kept + list(dict.fromkeys(requirement(f) for f in flags))

