#!/usr/bin/env python3
"""Build the release ZIP and prove the *archive* works, not the working tree.

Steps: regenerate MANIFEST.sha256 -> write ZIP -> extract into a clean folder
-> run the full test suite there -> run the installer against a throwaway
project. Any failure deletes the ZIP. Empty directories are never relied on.
Full subprocess output is retained beside the ZIP in <stem>-verification/run-*/.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "kh-software-factory"
EXCLUDED_PARTS = {".git", "__pycache__", "node_modules", ".pytest_cache", ".DS_Store", "dist"}
EXCLUDED_FILES = {"MANIFEST.sha256"}
# Local state never ships, whatever .gitignore says and whether or not this
# tree is a git checkout: credentials, per-user Claude settings, controller
# state and trace databases from running the framework in its own repository.
LOCAL_STATE_DIRS = {".kh", ".kh-observer", ".kh-backups"}
LOCAL_STATE_PREFIXES = ("adws/adw_data/",)
LOCAL_STATE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
                        ".pyc", ".pyo")


def local_state(rel: Path) -> bool:
    name, posix = rel.name, rel.as_posix()
    return (name == ".env" or (name.startswith(".env.") and name not in (".env.example", ".env.sample"))
            or posix == ".claude/settings.local.json"
            or any(part in LOCAL_STATE_DIRS for part in rel.parts)
            or posix.startswith(LOCAL_STATE_PREFIXES)
            or name.endswith(LOCAL_STATE_SUFFIXES))


def members():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if (path.is_file() and not any(p in EXCLUDED_PARTS for p in rel.parts)
                and rel.name not in EXCLUDED_FILES and not local_state(rel)):
            yield rel


def skipped_local_state():
    return sorted(rel.as_posix() for path in ROOT.rglob("*") if path.is_file()
                  for rel in [path.relative_to(ROOT)]
                  if not any(p in EXCLUDED_PARTS for p in rel.parts) and local_state(rel))


def write_manifest() -> Path:
    lines = [f"{hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()}  {rel.as_posix()}" for rel in members()]
    manifest = ROOT / "MANIFEST.sha256"
    manifest.write_text("\n".join(lines) + "\n")
    return manifest


def build(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for rel in [*members(), Path("MANIFEST.sha256")]:
            bundle.write(ROOT / rel, f"{NAME}/{rel.as_posix()}")


def captured_run(logs: Path, name: str, command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    (logs / f'{name}.stdout.log').write_text(result.stdout, encoding='utf-8')
    (logs / f'{name}.stderr.log').write_text(result.stderr, encoding='utf-8')
    return result


def verify(archive: Path) -> None:
    log_root = archive.with_name(archive.stem + '-verification')
    log_root.mkdir(parents=True, exist_ok=True)
    logs = Path(tempfile.mkdtemp(prefix='run-', dir=log_root))
    print(f'verification logs: {logs}', file=sys.stderr)
    # The default macOS temp spelling passes through /var, a system symlink.
    # The runtime intentionally refuses symlink parents for controller state.
    with tempfile.TemporaryDirectory(prefix="kh-release-",
                                     dir=Path(tempfile.gettempdir()).resolve()) as temporary:
        base = Path(temporary)
        test_temp = base / 'tmp'
        test_temp.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            bad = bundle.testzip()
            if bad:
                raise SystemExit(f"corrupt member: {bad}")
            bundle.extractall(base)
        package = base / NAME
        for line in (package / "MANIFEST.sha256").read_text().splitlines():
            expected, rel = line.split("  ", 1)
            actual = hashlib.sha256((package / rel).read_bytes()).hexdigest()
            if actual != expected:
                raise SystemExit(f"manifest mismatch: {rel}")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(test_temp)}
        tests = captured_run(logs, 'tests', [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
                             cwd=package, env=env)
        sys.stderr.write(tests.stderr[-4000:])
        if tests.returncode:
            raise SystemExit("test suite failed inside the extracted archive")
        evals = captured_run(logs, 'evals', [sys.executable, "-B", "scripts/run_evals.py"],
                             cwd=package, env=env)
        sys.stderr.write(evals.stdout[-2000:] + evals.stderr[-2000:])
        if evals.returncode:
            raise SystemExit("contract evals failed inside the extracted archive")
        project = base / "throwaway-project"
        project.mkdir()
        install = captured_run(logs, 'install', [sys.executable, str(package / "install.py"), "--project", str(project)],
                               env=env)
        if install.returncode or not (project / "adws/adw_start.py").is_file():
            raise SystemExit("installer failed from the extracted archive:\n" + install.stdout + install.stderr)
        # Exercise the stamped project command from its actual installed layout.
        command = [sys.executable, '-B', str(project / 'adws/adw_project.py')]
        for index, arguments in enumerate((['init', '--plan', 'project-plan.example.json', '--dry-run'],
                          ['init', '--plan', 'project-plan.example.json'], ['status'], ['next'], ['report'])):
            checked = captured_run(logs, f'project-{index + 1}', [*command, *arguments],
                                   cwd=project, env=env, timeout=30)
            if checked.returncode:
                raise SystemExit('Installed project tracking failed: ' + checked.stdout + checked.stderr)
        if not (project / '.kh/PROJECT_STATUS.md').is_file():
            raise SystemExit('Installed project tracking did not generate its report')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT.parent / f"{NAME}.zip"))
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    skipped = skipped_local_state()
    if skipped:
        print("not packaged (local state): " + ", ".join(skipped), file=sys.stderr)
    try:
        write_manifest()
        build(output)
        if not args.skip_verify:
            verify(output)
    except BaseException:
        # Any failure — SystemExit, OSError, KeyboardInterrupt — leaves no
        # archive behind that could be mistaken for a verified release.
        output.unlink(missing_ok=True)
        raise
    state = "built (verification SKIPPED — not a verified release)" if args.skip_verify else "built and verified"
    print(f"{state} {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
