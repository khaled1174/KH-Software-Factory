"""Floor guard: deterministic detection of moves that lower the quality bar.

Concept and rule set adapted from the floor guard in addyosmani/agent-skills
(`constraint-driven-development/references/floor-guard.md`, MIT). An agent that
hits a red check tends to take the cheapest road to green: skip or delete a
test, drop an assertion, silence a checker, leave a stub, or loosen a
threshold. Tightening the bar is silent; loosening it must be loud.

The guard reads the controller's own snapshots of the verified candidate, never
the builder's report. It does not block by itself: each finding becomes an
exact controller requirement, so the independent reviewer must justify it with
evidence and the verdict gate refuses approval while it is unmet. Findings name
the rule, the path, the line and the marker that matched — never the line's
text, which can hold a secret next to `gitleaks:allow` or `# nosec`.
"""
from __future__ import annotations

import difflib
import re
from pathlib import PurePosixPath

from .delta import VERIFICATION_PARTS
from .owner_review import _lines

PREFIX = "Floor guard"

TEST_NAME = re.compile(r"^(test_.*|.*_test\.[^.]+|.*\.test\.[^.]+|.*\.spec\.[^.]+|conftest\.py)$", re.I)

# 1. A checker silenced (any file). Each alternative names the marker reported.
SUPPRESSIONS = re.compile(
    r"#\s*noqa\b|#\s*type:\s*ignore|#\s*pyright:\s*ignore|#\s*pylint:\s*disable|#\s*pragma:\s*no\s*cover"
    r"|#\s*nosec\b|nosemgrep|gitleaks:allow|@ts-ignore|@ts-nocheck|@ts-expect-error|eslint-disable"
    r"|biome-ignore|istanbul\s+ignore|c8\s+ignore|Stryker\s+disable|//\s*nolint\b|#\[allow\(", re.I)
# 2. A test made easier (test files): skipped, expected to fail, or narrowed with `.only`.
SKIPS = re.compile(
    r"\.(skip|only|todo)\s*\(|\bx(it|describe|test)\s*\(|@pytest\.mark\.(skip|skipif|xfail)\b"
    r"|@unittest\.(skip|skipIf|skipUnless|expectedFailure)\b|\bpytest\.skip\s*\(|\.skipTest\s*\("
    r"|\bt\.Skip(Now)?\s*\(")
# 3. An assertion, counted per test file: fewer of them after the change is a finding.
ASSERTIONS = re.compile(r"\bassert\w*\b|\bexpect\s*\(|\.should\b|\bassert[A-Z]\w*\s*\(|\bt\.(Error|Fatal)f?\s*\(")
# 4. Unfinished work (non-test files). Case-sensitive on purpose: `TODO` the
# marker, not the word "todo" in a to-do app.
STUBS = re.compile(
    r"\braise\s+NotImplementedError\b|throw\s+new\s+\w*Error\s*\(\s*['\"`][^'\"`]*(?i:not\s+implemented)"
    r"|\bexcept(\s+[\w.(), ]+)?\s*:\s*(pass|\.\.\.)\s*(#.*)?$|catch\s*(\(\s*\w*\s*\))?\s*\{\s*\}"
    r"|\bTODO\b|\bFIXME\b|\bunimplemented!\s*\(|\btodo!\s*\(|\bpanic\s*\(\s*\"(?i:not\s+implemented)")
EXCEPT_OPEN = re.compile(r"^\s*except(\s+[\w.(), ]+)?\s*:\s*(#.*)?$")
# 5. A threshold loosened (check configuration files): minimums that went down,
# maximums that went up, or either removed.
MIN_KEYS = re.compile(r"(fail[_-]under|cov-fail-under|min(imum)?[_-]?coverage|coverage[_-]?threshold"
                      r"|\b(branches|statements|functions|lines)\b)\W{0,4}?[\s:=]+\s*(\d+(?:\.\d+)?)", re.I)
MAX_KEYS = re.compile(r"(max[_-]?warnings|max[_-]?complexity|max[_-]?line[_-]?length)\W{0,4}?[\s:=]+\s*(\d+(?:\.\d+)?)",
                      re.I)
THRESHOLD_FILES = re.compile(r"^(pyproject\.toml|setup\.cfg|tox\.ini|\.coveragerc|codecov\.ya?ml|\.nycrc.*"
                             r"|package\.json|jest\.config\..*|vitest\.config\..*|karma\.conf\..*|Makefile|justfile"
                             r"|\.eslintrc.*|eslint\.config\..*|\.flake8|ruff\.toml|\.pre-commit-config\.ya?ml)$", re.I)


def is_test_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and (any(p.lower() in VERIFICATION_PARTS for p in parts[:-1]) or bool(TEST_NAME.match(parts[-1])))


def _threshold_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and (bool(THRESHOLD_FILES.match(parts[-1])) or
                            (len(parts) > 2 and parts[0] == ".github" and parts[1] == "workflows"))


def _marker(match) -> str:
    return " ".join(match.group(0).split())[:40]


def _thresholds(lines, pattern, key_group, value_group):
    found = {}
    for number, text in lines:
        for match in pattern.finditer(text):
            found.setdefault(match.group(key_group).lower(), (number, float(match.group(value_group))))
    return found


def scan(baseline, current, paths) -> list[dict]:
    """Findings for the verified delta: rule, path, line, marker. Order is deterministic."""
    findings = []

    def flag(rule, path, line, marker):
        findings.append({"rule": rule, "path": path, "line": line, "marker": marker})

    for path in sorted(paths):
        before, after = baseline.entries.get(path), current.entries.get(path)
        old, new = _lines(before), _lines(after)
        test = is_test_file(path)
        if test and before is not None and before.kind == "file" and after is None:
            flag("test-deleted", path, 0, "file deleted")
            continue
        if old is None or new is None:
            continue
        added, removed = [], []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
            if tag in ("replace", "delete"):
                removed += [(i + 1, old[i].rstrip("\n")) for i in range(i1, i2)]
            if tag in ("replace", "insert"):
                added += [(j + 1, new[j].rstrip("\n")) for j in range(j1, j2)]
        previous = None
        for number, text in added:
            if (match := SUPPRESSIONS.search(text)):
                flag("silenced-checker", path, number, _marker(match))
            if test and (match := SKIPS.search(text)):
                flag("test-made-easier", path, number, _marker(match))
            if not test:
                if (match := STUBS.search(text)):
                    flag("unfinished-work", path, number, _marker(match))
                elif (text.strip() in ("pass", "...") and previous is not None
                      and previous[0] == number - 1 and EXCEPT_OPEN.match(previous[1])):
                    flag("unfinished-work", path, number, "except: " + text.strip())
            previous = (number, text)
        if test and after is not None:
            lost = sum(1 for _, t in removed if ASSERTIONS.search(t)) - sum(1 for _, t in added if ASSERTIONS.search(t))
            if lost > 0:
                first = next(n for n, t in removed if ASSERTIONS.search(t))
                flag("assertions-removed", path, first, f"{lost} fewer assertion line(s)")
        if _threshold_file(path):
            for pattern, key, value, loosened in ((MIN_KEYS, 1, 4, lambda was, now: now < was),
                                                  (MAX_KEYS, 1, 2, lambda was, now: now > was)):
                was = _thresholds(removed, pattern, key, value)
                now = _thresholds(added, pattern, key, value)
                for name, (line, number) in sorted(was.items()):
                    if name not in now:
                        if not _thresholds([(0, l) for l in new], pattern, key, value).get(name):
                            flag("threshold-removed", path, line, name)
                    elif loosened(number, now[name][1]):
                        flag("threshold-loosened", path, now[name][0], f"{name} {number:g} -> {now[name][1]:g}")
    return findings


def requirement(finding: dict) -> str:
    where = finding["path"] + (f":{finding['line']}" if finding["line"] else "")
    return (f"{PREFIX}: {finding['rule']} at {where} ({finding['marker']}) is justified by the task and does "
            f"not lower the quality bar; otherwise it is a blocking defect")


def replace_requirements(requirements: list[str], findings: list[dict]) -> list[str]:
    """The floor describes the current candidate: earlier floor requirements are replaced, not kept."""
    kept = [r for r in requirements if not r.startswith(PREFIX + ":")]
    return kept + list(dict.fromkeys(requirement(f) for f in findings))
