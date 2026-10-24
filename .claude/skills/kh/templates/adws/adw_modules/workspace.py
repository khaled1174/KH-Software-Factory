"""One isolated workspace per agent phase, then guarded promotion by controller.

Native CLI permissions still apply. Filesystem copying is not an OS/network
sandbox; it prevents ordinary worker writes or commits from touching owner work.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace

from . import permissions
from .process_io import cleanup_incomplete


def remember_role_cleanup(run, error=None, *, phase_id='', agent=''):
    """Carry an unconfirmed stop across permission errors and nested run views."""
    pending = getattr(run, 'role_cleanup_pending', None)
    if cleanup_incomplete(error):
        cleanup = getattr(error, 'kh_cleanup', None)
        if cleanup is None:
            cleanup = error.cleanup
        pending = {**(pending or {}), 'kh_cleanup': dict(cleanup)}
        pending.setdefault('phase_id', phase_id)
        pending.setdefault('agent', agent)
    if not pending:
        return None
    pending.setdefault('schema_version', 1)
    current, seen = run, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        current.role_cleanup_pending = pending
        current = vars(current).get('_owner')
    if error is not None:
        # Permission enforcement may replace the transport error. Keep the
        # original cleanup fact on whichever exception crosses this boundary.
        error.role_cleanup_pending = pending
        error.kh_cleanup = pending.get('kh_cleanup')
        error.resume_safe = False
        error.salvaged_paths = []
        if isinstance(pending.get('retained_workspace'), str):
            error.retained_workspace = pending['retained_workspace']
    return pending


def _saved_role_cleanup(run):
    """Read only this run's owner receipt; never adopt or signal recorded PIDs."""
    handoff = getattr(run, 'context_handoff_dir', None)
    if handoff is None:
        return None
    folder = Path(handoff) / 'role-cleanup-pending'
    receipt = folder
    try:
        try:
            folder.lstat()
        except FileNotFoundError:
            return None
        from .scope import _parent_fd
        # The same no-symlink walk used for scope state also protects every
        # parent of this controller receipt directory after a joined run.
        with _parent_fd(folder / '.entry') as (directory, _):
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 128:
                        raise ValueError('Too many role cleanup receipt entries')
                    if not entry.name.endswith('.json'):
                        raise ValueError('Unknown role cleanup receipt entry')
                    receipt = folder / entry.name
                    descriptor = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                         dir_fd=directory)
                    with os.fdopen(descriptor, 'rb') as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                            raise ValueError('Role cleanup receipt must be a bounded regular file')
                        data = stream.read(65537)
                    if len(data) > 65536:
                        raise ValueError('Role cleanup receipt exceeds size limit')
                    value = json.loads(data.decode('utf-8'))
                    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or
                            value['schema_version'] != 1 or
                            not isinstance(value.get('kh_cleanup'), dict) or
                            value['kh_cleanup'].get('complete') is not False or
                            any(not isinstance(value.get(key), str) or not value[key]
                                for key in ('phase_id', 'agent', 'retained_workspace', 'retained_directory')) or
                            not Path(value['retained_workspace']).is_absolute() or
                            not Path(value['retained_directory']).is_absolute()):
                        raise ValueError('Unknown or malformed pending role cleanup receipt')
                    return {**value, 'pending_receipt': str(receipt)}
    except Exception as problem:
        # Unknown state requires owner resolution. A restart cannot turn an
        # unreadable receipt into authority to dispatch or delete resources.
        return {'schema_version': 1, 'kh_cleanup': {'complete': False},
                'pending_receipt': str(receipt), 'receipt_error': str(problem)[:500]}
    return None


def require_role_cleanup(run):
    if not getattr(run, 'role_cleanup_pending', None):
        saved = _saved_role_cleanup(run)
        if saved:
            run.role_cleanup_pending = saved
    if getattr(run, 'role_cleanup_pending', None):
        error = RuntimeError('ROLE_CLEANUP_PENDING: stop the retained role before another send or import')
        remember_role_cleanup(run, error)
        raise error


def exclusions(run):
    root = Path(run.repo_root).resolve()
    paths = [Path(".kh-observer"), Path(run.cfg.defaults.data_dir) / "sessions", Path(run.cfg.observability.db),
             Path(str(run.cfg.observability.db) + "-wal"), Path(str(run.cfg.observability.db) + "-shm")]
    paths += [Path('.kh') / p for p in ('project.db', 'project.db-wal', 'project.db-shm',
              'project-status.json', 'PROJECT_STATUS.md', 'task-reports', 'task-receipts',
              'controller.lock', 'controller-control.lock', 'controller-state.json',
              'controller-request.json', 'intent-digests.json', 'intent-digests.lock', 'work',
              'timing-history.json', 'timing-history.tmp', 'dispositions.json')]
    paths.append(Path('.kh/model-switch'))  # owner steering is controller state, never source
    paths += [Path('.kh') / p for p in ('parallel', 'execution-decision.json',
                                      'execution-metadata.json')]
    # Credentials never enter an isolated workspace: adapters authenticate
    # through the process environment, not through a copied file.
    paths += [Path('.env'), Path('.kh-backups')]
    paths += [p.relative_to(root) for p in root.glob('.env.*')]
    result = []
    for path in paths:
        absolute = path if path.is_absolute() else root / path
        try: result.append(absolute.relative_to(root).as_posix())
        except ValueError: pass
    return result


class RunView:
    def __init__(self, owner, **values):
        self._owner = owner
        self.__dict__.update(values)

    def __getattr__(self, name):
        return getattr(self._owner, name)


class AgentWorkspace:
    def __init__(self, run, phase, agent):
        self.owner, self.phase, self.agent = run, phase, agent
        self.source = Path(run.repo_root).resolve()
        self.capture_view = SimpleNamespace(repo_root=self.source, cfg=run.cfg,
                                            snapshot_excludes=exclusions(run))
        self.baseline = permissions.snapshot(self.capture_view)
        self.dependency_baseline = permissions.dependency_fingerprint(self.capture_view)
        for rel, entry in self.baseline.entries.items():
            if entry.kind == "symlink" and not rel.startswith("@git/"):
                target = (self.source / rel).resolve()
                if not target.is_relative_to(self.source):
                    raise permissions.PermissionBreach(f"Input symlink escapes project: {rel}")
        self.temp = tempfile.TemporaryDirectory(prefix="kh-agent-")
        self.root = (Path(self.temp.name) / "workspace").resolve()
        self.root.mkdir()
        try:
            if any(p.startswith("@git/") for p in self.baseline):
                subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--no-local",
                                str(self.source), str(self.root)], check=True, capture_output=True)
                subprocess.run(["git", "remote", "remove", "origin"], cwd=self.root,
                               check=True, capture_output=True)
                # Remove checked-out paths absent from the owner's current tree.
                for p in sorted(self.root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                    rel = p.relative_to(self.root)
                    if ".git" in rel.parts: continue
                    if rel.as_posix() not in self.baseline:
                        if p.is_dir() and not p.is_symlink():
                            try: p.rmdir()
                            except OSError: pass
                        else: p.unlink()
            for rel, entry in sorted(self.baseline.entries.items(), key=lambda x: x[0].count("/")):
                if rel.startswith("@git/"): continue
                if entry.kind == "symlink":
                    # Convert absolute in-project aliases to relative worker aliases.
                    target = (self.source / rel).resolve().relative_to(self.source)
                    relative_target = os.path.relpath(self.root / target, (self.root / rel).parent)
                    entry = permissions.Entry("symlink", entry.mode, relative_target.encode())
                permissions.restore_entry(self.root / rel, entry)
            # Copy declared caches/dependencies independently, without holding
            # their bytes in source snapshots or importing their later output.
            # Never share a writable dependency directory with the owner.
            for rel in permissions.generated_roots(self.capture_view):
                source = self.source / rel
                for current, dirs, files in os.walk(source, followlinks=False):
                    for name in dirs + files:
                        path = Path(current) / name
                        if path.is_symlink() and not path.resolve().is_relative_to(self.source):
                            raise permissions.PermissionBreach(f'Cache symlink escapes project: {path}')
                        permissions._capture(path) if not path.is_file() else None
                shutil.copytree(source, self.root / rel, symlinks=True, dirs_exist_ok=True)
                for current, dirs, files in os.walk(self.root / rel, followlinks=False):
                    for name in dirs + files:
                        path = Path(current) / name
                        if path.is_symlink() and Path(os.readlink(path)).is_absolute():
                            target = (self.source / path.relative_to(self.root)).resolve().relative_to(self.source)
                            path.unlink()
                            path.symlink_to(os.path.relpath(self.root / target, path.parent))
            data = Path(run.cfg.defaults.data_dir)
            if data.is_absolute():
                try: data = data.relative_to(self.source)
                except ValueError: data = Path("adws/adw_data")
            handoff_rel = data / "sessions" / run.adw_id / "context_handoff"
            handoff = self.root / handoff_rel
            handoff.mkdir(parents=True, exist_ok=True)
            if Path(run.context_handoff_dir).exists():
                if Path(run.context_handoff_dir).is_symlink() or any(
                    p.is_symlink() for p in Path(run.context_handoff_dir).rglob("*")
                ):
                    raise permissions.PermissionBreach("Prior evidence must not contain symlinks")
                shutil.copytree(run.context_handoff_dir, handoff, dirs_exist_ok=True, symlinks=True)
            output_rel = handoff_rel / phase.phase_id / agent.name
            (self.root / output_rel).mkdir(parents=True, exist_ok=True)
            self.view = RunView(run, repo_root=self.root,
                                context_handoff_dir=self.root / output_rel,
                                agent_output_relative=output_rel.as_posix(),
                                handoff_root=handoff,
                                isolated=True, snapshot_excludes=[],
                                promote=self.promote,
                                source_baseline=self.digest())
            self.promoted = False
        except BaseException:
            self.temp.cleanup()
            raise

    def digest(self):
        return hashlib.sha256(json.dumps(self.baseline, sort_keys=True).encode()).hexdigest()

    def remap_input(self, call):
        source, target = str(self.source), str(self.root)
        replacements = {source: target}
        for ensure_ascii in (False, True):
            replacements.setdefault(json.dumps(source, ensure_ascii=ensure_ascii)[1:-1],
                                    json.dumps(target, ensure_ascii=ensure_ascii)[1:-1])
        pattern = '|'.join(re.escape(value) for value in sorted(replacements, key=len, reverse=True))
        def remap_text(text):
            # One pass also handles JSON-escaped paths without remapping output.
            return re.sub(pattern, lambda match: replacements[match.group()], text)
        result = copy.copy(call)
        result.prompt = remap_text(call.prompt)
        if call.previous:
            data = remap_text(call.previous.model_dump_json())
            result.previous = type(call.previous).model_validate_json(data)
        if getattr(call, "plan", None):
            data = remap_text(call.plan.model_dump_json())
            result.plan = type(call.plan).model_validate_json(data)
        return result

    def promote(self, envelope):
        self._import_changes()
        source = json.dumps(str(self.root), ensure_ascii=False)[1:-1]
        target = json.dumps(str(self.source), ensure_ascii=False)[1:-1]
        raw = envelope.model_dump_json().replace(source, target)
        return type(envelope).model_validate_json(raw)

    def salvage(self):
        """Import a failed phase's permitted changes, under every check a promotion makes.

        Used only for a builder that stopped before reporting (timeout, provider
        failure), and only into the task's staging copy, never the owner tree:
        the work then waits there, unaccepted, for the next run to continue.
        Returns the imported paths, or None when nothing could be taken safely.
        """
        require_role_cleanup(self.view)
        # The phase's own baseline, set when its first send began; the stage
        # view's would be found through RunView's fallback and is the wrong one.
        if "agent_baseline" not in vars(self.view):
            return None
        try:
            return self._import_changes()
        except permissions.PermissionBreach as breach:
            # A phase that broke its boundary contributes nothing. The paths it
            # was refused are named, so the next run's brief can say that their
            # edits were not kept and they are still at the owner baseline (SYS-06).
            self.rejected = [{'path': p, 'reason': str(breach)[:500]}
                             for p in getattr(breach, 'paths', None) or [] if not p.startswith('@git/')]
            return None

    def _import_changes(self):
        require_role_cleanup(self.view)
        # Gate baseline was captured after workspace initialization, before the
        # first send. All corrections are checked against that same baseline.
        current = permissions.snapshot(self.view)
        touched = permissions.changed_paths(self.view.agent_baseline, current)
        permissions.enforce(self.view, self.phase, self.agent, self.view.agent_baseline)
        if permissions.dependency_fingerprint(self.view) != self.dependency_baseline:
            raise permissions.PermissionBreach('Dependency inputs changed in staging; prepare dependencies before guarded execution')
        owner_now = permissions.snapshot(self.capture_view)
        if (owner_now != self.baseline or
                permissions.dependency_fingerprint(self.capture_view) != self.dependency_baseline):
            raise permissions.PermissionBreach("Owner workspace changed during execution; no worker changes imported")
        imported = [p for p in touched if not p.startswith("@git/")]
        for rel in imported:
            entry = current.entries.get(rel)
            if entry and entry.kind == "symlink":
                target = os.readlink(self.root / rel)
                resolved = (self.root / rel).parent.joinpath(target).resolve()
                if Path(target).is_absolute() or not resolved.is_relative_to(self.root):
                    raise permissions.PermissionBreach(f"Refusing to import escaping symlink: {rel}")
        # Record pre-import bytes, including runtime outputs excluded above.
        originals = {}
        for rel in imported:
            target = self.source / rel
            originals[rel] = permissions._capture(target) if target.exists() or target.is_symlink() else None
        try:
            for rel in sorted(imported, key=lambda p: p.count("/"), reverse=True):
                if rel not in current.entries:
                    permissions.restore_entry(self.source / rel, None)
            for rel in sorted(imported, key=lambda p: p.count("/")):
                if rel in current.entries:
                    permissions.restore_entry(self.source / rel, current.entries[rel])
        except BaseException:
            for rel in sorted(imported, key=lambda p: p.count("/"), reverse=True):
                if originals[rel] is None:
                    permissions.restore_entry(self.source / rel, None)
            for rel in sorted(imported, key=lambda p: p.count("/")):
                if originals[rel] is not None:
                    permissions.restore_entry(self.source / rel, originals[rel])
            raise
        self.promoted = True
        return imported

    def __enter__(self):
        return self

    def __exit__(self, _type, error, _traceback):
        pending = remember_role_cleanup(self.view, error,
            phase_id=self.phase.phase_id, agent=self.agent.name)
        if not pending:
            self.temp.cleanup()
            return
        # CPython TemporaryDirectory exposes no public "retain" method. Its
        # weakref finalizer must be detached before any receipt I/O, otherwise
        # garbage collection would delete a still-owned CWD after this return.
        self.temp._finalizer.detach()
        pending.update(retained_workspace=str(self.root), retained_directory=self.temp.name)
        retained = getattr(self.owner, 'retained_workspaces', None)
        if retained is None:
            retained = []
            self.owner.retained_workspaces = retained
        if not any(item.get('retained_workspace') == str(self.root) for item in retained):
            retained.append(dict(pending))
        if error is not None:
            error.retained_workspace = str(self.root)
        from .scope import _atomic_json
        name = self.phase.phase_id + '-' + self.agent.name
        name = ''.join(c if c.isalnum() or c in '._-' else '_' for c in name)[:160]
        receipt = Path(self.owner.context_handoff_dir) / 'role-cleanup-pending' / (name + '.json')
        try:
            _atomic_json(receipt, pending)
        except Exception as problem:
            # Retention is already effective even if owner evidence is unwritable.
            pending['receipt_error'] = str(problem)[:500]
            if error is not None and hasattr(error, 'add_note'):
                error.add_note('Retained role workspace; cleanup receipt failed: ' + str(receipt))
