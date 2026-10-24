#!/usr/bin/env python3
"""Deterministic contract evals for the KH instruction surface.

The AI-native SDLC play for evals: when CLAUDE.md, skills, hooks, or command
contracts change, a fixed suite says whether the framework still meets its
standard. Live model quality is out of scope here (tracked as external
validation); these cases pin the *enforceable contracts* the framework ships:
command inventory, skill frontmatter, protected-path integrity and
self-protection, hook fail-closed behavior, propose-only audit skills,
observer promotion gating, the intent template, and packaging hygiene.

Each case is a JSON file in evals/: {"id", "title", "check": {"type", ...}}.
The runner is non-interactive, prints one line per case, and exits non-zero
when the pass rate falls below --threshold (default: 100). Cases are fixed
before a run; a failing case is a finding, not a prompt to edit the case.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"


def fail(message: str):
    return False, message


def ok(message: str = ""):
    return True, message


def check_files_exist(spec):
    missing = [p for p in spec["paths"] if not (ROOT / p).is_file()]
    return fail(f"missing: {missing}") if missing else ok(f"{len(spec['paths'])} files present")


def check_glob_count(spec):
    found = sorted(ROOT.glob(spec["pattern"]))
    expected = spec["expected"]
    if len(found) != expected:
        return fail(f"{spec['pattern']}: found {len(found)}, expected {expected}")
    return ok(f"{len(found)} == {expected}")


def check_files_documented_in(spec):
    document = (ROOT / spec["document"]).read_text(encoding="utf-8")
    if spec.get("match") == "stem":
        missing = [p.name for p in sorted(ROOT.glob(spec["glob"])) if p.stem not in document]
    else:
        missing = [p.name for p in sorted(ROOT.glob(spec["glob"])) if p.name not in document]
    return fail(f"not documented in {spec['document']}: {missing}") if missing else ok()


def check_contains(spec):
    text = (ROOT / spec["path"]).read_text(encoding="utf-8")
    missing = [n for n in spec["needles"] if n not in text]
    return fail(f"{spec['path']} lacks: {missing}") if missing else ok()


def check_not_contains(spec):
    text = (ROOT / spec["path"]).read_text(encoding="utf-8")
    hits = [p for p in spec["patterns"] if re.search(p, text, re.IGNORECASE | re.DOTALL)]
    return fail(f"{spec['path']} contains forbidden patterns: {hits}") if hits else ok()


def frontmatter_keys(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    keys = []
    for line in lines[1:]:
        if line.strip() == "---":
            return keys
        match = re.match(r"^([A-Za-z][A-Za-z-]*):", line)
        if match:
            keys.append(match.group(1))
    return None


def check_frontmatter(spec):
    problems = []
    files = sorted(ROOT.glob(spec["glob"]))
    if not files:
        return fail(f"no files match {spec['glob']}")
    required = set(spec.get("required", []))
    allowed = set(spec.get("allowed", []))
    for path in files:
        keys = frontmatter_keys(path)
        if keys is None:
            problems.append(f"{path.relative_to(ROOT)}: no frontmatter block")
            continue
        missing = required - set(keys)
        extra = set(keys) - allowed
        if missing:
            problems.append(f"{path.relative_to(ROOT)}: missing {sorted(missing)}")
        if extra:
            problems.append(f"{path.relative_to(ROOT)}: disallowed fields {sorted(extra)}")
    return fail("; ".join(problems)) if problems else ok(f"{len(files)} files conform")


def protected_entries(spec):
    entries = []
    problems = []
    for raw in (ROOT / spec["path"]).read_text(encoding="utf-8").splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        candidate = Path(entry)
        if candidate.is_absolute() or ".." in candidate.parts:
            problems.append(f"invalid entry: {entry}")
            continue
        if not (ROOT / entry.rstrip("/")).exists():
            problems.append(f"entry does not exist: {entry}")
            continue
        entries.append(entry)
    return entries, problems


def check_protected_paths(spec):
    entries, problems = protected_entries(spec)
    missing = [r for r in spec.get("required", []) if r not in entries]
    if missing:
        problems.append(f"required entries absent: {missing}")
    if problems:
        return fail("; ".join(problems))
    return ok(f"{len(entries)} entries valid")


def check_mirrored_in(spec):
    entries, problems = protected_entries({"path": spec["list_path"]})
    if problems:
        return fail("; ".join(problems))
    document = (ROOT / spec["document"]).read_text(encoding="utf-8")
    missing = [e for e in entries if e not in document]
    return fail(f"entries not mirrored in {spec['document']}: {missing}") if missing else ok(
        f"{len(entries)} entries mirrored")


def check_hook_behavior(spec):
    hook_source = (ROOT / spec["hook"]).read_text(encoding="utf-8")
    results = []

    def run_hook(project: Path, target: str, extra_env=None):
        payload = json.dumps({"tool_input": {"file_path": target}})
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), **(extra_env or {})}
        # Relative tool paths resolve against the invocation cwd, as in a real session.
        return subprocess.run(["bash", str(project / ".claude/hooks/protect-paths.sh")],
                              input=payload, capture_output=True, text=True, env=env,
                              cwd=project).returncode

    with tempfile.TemporaryDirectory(prefix="kh-eval-hook-") as temporary:
        project = Path(temporary)
        (project / ".claude/hooks").mkdir(parents=True)
        (project / ".claude/hooks/protect-paths.sh").write_text(hook_source, encoding="utf-8")
        (project / "protected.txt").write_text("issued record", encoding="utf-8")
        (project / "open.txt").write_text("living file", encoding="utf-8")
        (project / ".claude/protected-paths.txt").write_text("protected.txt\n", encoding="utf-8")
        results.append(("blocks protected path", run_hook(project, "protected.txt") == 2))
        results.append(("allows unprotected path", run_hook(project, "open.txt") == 0))
        # Invalid configuration must fail closed, never silently permit.
        (project / ".claude/protected-paths.txt").write_text("/etc\n", encoding="utf-8")
        results.append(("fails closed on invalid entry", run_hook(project, "open.txt") == 2))
        # An empty or comment-only list is a broken configuration, not an
        # empty protection set: it must fail closed too.
        (project / ".claude/protected-paths.txt").write_text("", encoding="utf-8")
        results.append(("fails closed on empty list", run_hook(project, "open.txt") == 2))
        (project / ".claude/protected-paths.txt").write_text("# only a comment\n", encoding="utf-8")
        results.append(("fails closed on comment-only list", run_hook(project, "open.txt") == 2))
        # macOS filesystems compare case-insensitively; the platform override
        # lets this host exercise that comparison.
        (project / ".claude/protected-paths.txt").write_text("docs/protected.md\n", encoding="utf-8")
        darwin = {"KH_HOOK_PLATFORM": "darwin"}
        results.append(("darwin blocks case-variant protected path",
                        run_hook(project, "Docs/PROTECTED.MD", darwin) == 2))
        results.append(("darwin allows unprotected path",
                        run_hook(project, "open.txt", darwin) == 0))
    failed = [name for name, passed in results if not passed]
    return fail(f"hook behavior: {failed}") if failed else ok(
        "block/allow/fail-closed/case-fold verified")


def check_manifest_verifies(spec):
    mismatches = []
    for line in (ROOT / spec["manifest"]).read_text(encoding="utf-8").splitlines():
        expected, rel = line.split("  ", 1)
        target = ROOT / rel
        if not target.is_file():
            mismatches.append(f"missing {rel}")
            continue
        import hashlib
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            mismatches.append(f"hash mismatch {rel}")
    return fail(f"manifest: {len(mismatches)} problems; first: {mismatches[:2]}") if mismatches else ok()


def check_no_secret_values(spec):
    problems = []
    for raw in (ROOT / spec["path"]).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip():
            problems.append(f"{key.strip()} has a value")
    text = (ROOT / spec["path"]).read_text(encoding="utf-8")
    if re.search(r"sk-[A-Za-z0-9]", text):
        problems.append("token-shaped literal present")
    return fail(f"{spec['path']}: {problems}") if problems else ok()


def check_links_exist(spec):
    text = (ROOT / spec["document"]).read_text(encoding="utf-8")
    links = sorted(set(re.findall(spec["link_pattern"], text)))
    missing = [link for link in links if not (ROOT / link).is_file()]
    return fail(f"{spec['document']} links to missing files: {missing}") if missing else ok(
        f"{len(links)} links resolve")


CHECKS = {
    "files_exist": check_files_exist,
    "glob_count": check_glob_count,
    "files_documented_in": check_files_documented_in,
    "contains": check_contains,
    "not_contains": check_not_contains,
    "frontmatter": check_frontmatter,
    "protected_paths": check_protected_paths,
    "mirrored_in": check_mirrored_in,
    "hook_behavior": check_hook_behavior,
    "manifest_verifies": check_manifest_verifies,
    "no_secret_values": check_no_secret_values,
    "links_exist": check_links_exist,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=100.0,
                        help="minimum pass percentage required to exit zero")
    args = parser.parse_args()
    cases = sorted(EVALS.glob("*.json"))
    if not cases:
        print("no eval cases found", file=sys.stderr)
        return 2
    passed = 0
    for case_path in cases:
        case = json.loads(case_path.read_text(encoding="utf-8"))
        check = case["check"]
        handler = CHECKS.get(check["type"])
        if handler is None:
            success, detail = fail(f"unknown check type: {check['type']}")
        else:
            success, detail = handler(check)
        passed += 1 if success else 0
        status = "PASS" if success else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"{status} {case['id']}: {case['title']}{suffix}")
    rate = 100.0 * passed / len(cases)
    print(f"\n{passed}/{len(cases)} passed ({rate:.1f}%), threshold {args.threshold:.1f}%")
    return 0 if rate >= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
