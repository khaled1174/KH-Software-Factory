"""Content-based promotion boundary. This is not an OS or network sandbox.

Capture real bytes, modes, symlinks and Git control state. Workers execute in a
separate workspace; only permitted, verified changes are imported by controller.
"""
from __future__ import annotations
import hashlib
import os
import stat
import subprocess
from pathlib import Path
from dataclasses import dataclass
import re

class PermissionBreach(RuntimeError):
    pass

@dataclass
class Entry:
    kind: str
    mode: int
    data: bytes
    @property
    def fingerprint(self):
        return hashlib.sha256(self.kind.encode() + str(self.mode).encode() + self.data).hexdigest()

class Snapshot(dict):
    def __init__(self, entries, root, git_paths):
        super().__init__({p: e.fingerprint for p, e in entries.items()})
        self.entries, self.root, self.git_paths = entries, root, git_paths

def _capture(path):
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return Entry("symlink", mode, os.readlink(path).encode())
    if stat.S_ISREG(info.st_mode):
        return Entry("file", mode, path.read_bytes())
    if stat.S_ISDIR(info.st_mode):
        return Entry("directory", mode, b"")
    raise PermissionBreach(f"Unsupported filesystem entry: {path}")

def generated_roots(run, *, dependencies_only=False):
    """Declared untracked caches are disposable, never promoted as source.

    Do not infer this from .gitignore: arbitrary ignored owner files remain
    protected. A directory containing even one tracked file remains source.
    """
    root = Path(run.repo_root).resolve()
    defaults = getattr(getattr(run, 'cfg', None), 'defaults', None)
    dependencies = getattr(defaults, 'dependency_paths', [])
    patterns = dependencies if dependencies_only else [*getattr(defaults, 'generated_paths', []), *dependencies]
    if not patterns:
        return []
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True)
    tracked_paths = tracked.stdout.decode('utf-8', errors='surrogateescape').split('\0') if tracked.returncode == 0 else []
    result = []
    for current, dirs, _ in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ('.git', '.kh')]
        for name in list(dirs):
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                continue
            if any(_glob(p.rstrip('/')).fullmatch(rel) for p in patterns):
                if any(p == rel or p.startswith(rel + '/') for p in tracked_paths):
                    continue
                result.append(rel)
                dirs.remove(name)
    return result

def dependency_fingerprint(run):
    """Hash declared untracked inputs without retaining dependency file bytes.

    Dependencies are copied independently and never promoted. Their contents
    must stay stable throughout verification and in the owner during execution.
    """
    root = Path(run.repo_root).resolve()
    digest = hashlib.sha256()
    for rel in sorted(generated_roots(run, dependencies_only=True)):
        base = root / rel
        for current, dirs, files in os.walk(base, followlinks=False):
            dirs.sort()
            for name in sorted(dirs + files):
                path = Path(current) / name
                info = path.lstat()
                key = path.relative_to(root).as_posix()
                digest.update((key + '\0' + str(stat.S_IMODE(info.st_mode)) + '\0').encode())
                if stat.S_ISLNK(info.st_mode):
                    target = path.resolve()
                    if not target.is_relative_to(root):
                        raise PermissionBreach(f'Dependency symlink escapes project: {key}')
                    digest.update(b'L' + target.relative_to(root).as_posix().encode() + b'\0')
                elif stat.S_ISDIR(info.st_mode):
                    digest.update(b'D\0')
                elif stat.S_ISREG(info.st_mode):
                    content = hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            content.update(block)
                    digest.update(b'F' + content.digest())
                else:
                    raise PermissionBreach(f'Unsupported dependency entry: {key}')
        # Include even an empty declared dependency root and its mode.
        digest.update((rel + '\0' + str(stat.S_IMODE(base.stat().st_mode)) + '\0').encode())
    return digest.hexdigest()


def snapshot(run):
    root = Path(run.repo_root).resolve()
    if not root.is_dir():
        raise PermissionBreach(f"Missing workspace: {root}")
    entries, git_paths = {}, {}
    excluded = [*getattr(run, "snapshot_excludes", []), *generated_roots(run)]
    total = 0
    limit = getattr(getattr(getattr(run, "cfg", None), "defaults", None),
                    "snapshot_max_bytes", 536870912)
    for current, dirs, files in os.walk(root, followlinks=False):
        relative = Path(current).relative_to(root)
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in list(dirs) + files:
            rel = (relative / name).as_posix()
            if rel == ".git" or any(rel == x.rstrip("/") or rel.startswith(x.rstrip("/")+"/") for x in excluded):
                if name in dirs: dirs.remove(name)
                continue
            entry = _capture(root / rel)
            total += len(entry.data)
            if total > limit:
                raise PermissionBreach(f"Snapshot exceeds {limit} bytes; configure snapshot_max_bytes before running")
            entries[rel] = entry
    # A non-Git directory is supported; an invalid .git must not look clean.
    proc = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=root, capture_output=True, text=True)
    if proc.returncode:
        if (root / ".git").exists():
            raise PermissionBreach("Cannot inspect Git control state: " + proc.stderr.strip())
    else:
        gd = Path(proc.stdout.strip())
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=root, capture_output=True, text=True, check=True)
        cd = Path(common.stdout.strip())
        if not cd.is_absolute(): cd = root / cd
        candidates = [("HEAD", gd / "HEAD"), ("index", gd / "index"),
                      ("config", cd / "config"), ("packed-refs", cd / "packed-refs")]
        if (cd / "refs").exists():
            candidates += [("refs/"+f.relative_to(cd/"refs").as_posix(), f)
                           for f in (cd/"refs").rglob("*") if f.is_file()]
        for key, file in candidates:
            logical = "@git/" + key
            git_paths[logical] = file
            if file.exists(): entries[logical] = _capture(file)
    result = Snapshot(entries, root, git_paths)
    if "@git/index" in result:
        index = subprocess.run(["git", "ls-files", "--stage", "-v", "-z"], cwd=root,
                               capture_output=True, check=True).stdout
        result["@git/index"] = hashlib.sha256(index).hexdigest()
    return result

def changed_paths(before, after):
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))

def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path == pattern.rstrip("/") or path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def covers(path: str, pattern: str) -> bool:
    """Inclusive matching for rules that must never under-select.

    Verification adapters and light-mode risk rules fail open when a pattern
    misses, so a path counts as covered if the project glob matches it (`**/`
    spans zero or more directories), the historic fnmatch reading matches it,
    or a wildcard `dir/` pattern contains it.
    """
    import fnmatch
    if _matches(path, pattern) or fnmatch.fnmatchcase(path, pattern):
        return True
    return pattern.endswith("/") and any(c in pattern for c in "*?") and _matches(path, pattern + "**")


def always_writable(cfg):
    return []  # No shared data-directory exemption.


def structural_directory(path, before, after) -> bool:
    """True only when a directory itself appears or disappears.

    Its contents are judged path by path, so the bare directory adds nothing.
    A kind change at a path (file <-> directory) or a mode change on a
    directory that already existed is a real change to owner state and must be
    scoped, reviewed and diffed like any file.
    """
    b, a = before.entries.get(path), after.entries.get(path)
    return ((b is None and a is not None and a.kind == "directory")
            or (a is None and b is not None and b.kind == "directory"))


def _trace_db_paths(cfg):
    """The live trace DB and its WAL/SHM files are controller state, never source."""
    import posixpath
    db = cfg.observability.db
    if not db or posixpath.isabs(db):
        return []
    db = posixpath.normpath(db)
    return [db, db + "-wal", db + "-shm"]

def permitted(path, agent, cfg):
    if Path(path).name == ".env" or Path(path).name.startswith(".env."):
        return False
    if path.startswith("@git/") or path == ".git" or path.startswith(".git/"):
        return False
    controls = ["adws/adw_data/prompt_engineering/", "adws/adw_data/harness_engineering/",
                cfg.defaults.data_dir.rstrip("/") + "/sessions/", ".claude/", ".gemini/", ".kh/",
                ".env", ".env.*", ".kh-observer/", ".kh-backups/",
                *_trace_db_paths(cfg)]
    # Control definitions and prior evidence are controller-owned, even when
    # an agent explicitly asks for a broad write pattern.
    if any(_matches(path, p) for p in [*controls, *cfg.defaults.protected_files]):
        return False
    return agent.writes is None or any(_matches(path, p) for p in agent.writes)

def restore_entry(path, entry):
    # Never follow a replaced parent symlink when restoring/promoting a path.
    for parent in path.parents:
        if parent.is_symlink():
            raise PermissionBreach(f"Symlink parent prevents safe restore: {parent}")
    if path.is_symlink() or (path.exists() and not path.is_dir()): path.unlink()
    if entry is None:
        if path.is_dir(): path.rmdir()
        return
    if entry.kind == "directory":
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(entry.mode)
    elif entry.kind == "symlink":
        if path.is_dir(): path.rmdir()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(entry.data.decode())
    else:
        if path.is_dir(): path.rmdir()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream: stream.write(entry.data)
        path.chmod(entry.mode)

def enforce(run, phase, agent, before):
    after = snapshot(run)
    touched = changed_paths(before, after)
    output = getattr(run, "agent_output_relative", None)
    contract = getattr(run, "scope_contract", None)
    if contract is not None:
        from .scope import check_paths
        file_paths = [p for p in touched if not structural_directory(p, before, after)]
        check_paths(contract, file_paths, output)
    breaches = []
    for path in touched:
        own = output and (path == output or path.startswith(output + "/"))
        # New parent directories are legitimate only for this agent's output.
        own_parent = output and output.startswith(path + "/") and path not in before
        entry = after.entries.get(path)
        structural = (path not in before and entry is not None and entry.kind == "directory"
                      and any(child.startswith(path + "/") and permitted(child, agent, run.cfg)
                              for child in touched))
        if not (own or own_parent or structural or permitted(path, agent, run.cfg)):
            breaches.append(path)
    if breaches:
        # Isolated execution discards the whole workspace on a breach. For
        # direct diagnostic callers, restore captured owner content faithfully.
        if not getattr(run, "isolated", False):
            for path in sorted(breaches, key=lambda p: p.count("/"), reverse=True):
                target = before.git_paths.get(path) or after.git_paths.get(path) or before.root / path
                restore_entry(target, before.entries.get(path))
        error = PermissionBreach(f"{agent.name} modified prohibited paths: " + ", ".join(breaches))
        error.paths = list(breaches)
        raise error
    return touched
