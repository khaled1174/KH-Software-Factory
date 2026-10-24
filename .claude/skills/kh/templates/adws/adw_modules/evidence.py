"""Publish staged task evidence without importing any candidate source changes.

Only derived artifact references are rebased. Exact pre-rewrite records are kept
separately, and no raw stream, prompt, command text, verdict or status is edited.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from .scope import ScopeViolation, _atomic_json, _parent_fd, _read_json

KEYS = {'artifacts', 'output_artifact', 'document_path'}


def _read(path):
    with _parent_fd(Path(path)) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ScopeViolation(f'Evidence is not a regular file: {path}')
            return stream.read()


def _files(root, select=None):
    """Snapshot regular files through directory handles, never symlink traversal."""
    result = {}
    def walk(descriptor, prefix):
        for name in sorted(os.listdir(descriptor)):
            relative = prefix / name
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try: walk(child, relative)
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if select is not None and not select(relative): continue
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(child, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ScopeViolation(f'Unexpected evidence entry: {relative}')
                    result[relative] = stream.read()
            else:
                raise ScopeViolation(f'Refusing symlink or special evidence entry: {relative}')
    with _parent_fd(Path(root)) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try: walk(descriptor, Path())
        finally: os.close(descriptor)
    return result


def _write_new(path, data):
    """Publish a complete new file atomically, refusing conflicting old content."""
    with _parent_fd(Path(path), create=True) as (parent, name):
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode) or _read(path) != data:
                raise ScopeViolation(f'Existing controller evidence differs: {path}')
            return
        temporary = f'.{name}.{secrets.token_hex(12)}.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link publication refuses any concurrently created target.
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        finally:
            os.unlink(temporary, dir_fd=parent)


def _replace_bytes(path, data):
    """Replace derived JSONL after its exact original has been preserved."""
    with _parent_fd(Path(path)) as (parent, name):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ScopeViolation(f'Refusing non-regular derived record: {path}')
        temporary = f'.{name}.{secrets.token_hex(12)}.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try: os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError: pass


def _rebase(value, staging_root, owner_root):
    def path(value):
        if not isinstance(value, str) or not Path(value).is_absolute(): return value
        try: relative = Path(value).relative_to(staging_root)
        except ValueError: return value
        if '..' in relative.parts: return value
        return str(owner_root / relative)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in KEYS:
                result[key] = [path(x) for x in item] if isinstance(item, list) else path(item)
            else:
                result[key] = _rebase(item, staging_root, owner_root)
        return result
    if isinstance(value, list):
        return [_rebase(x, staging_root, owner_root) for x in value]
    return value


def publish_staged_evidence(run, view, *, status='candidate'):
    """Durably copy task evidence and rebase derived references, on any outcome.

    ``status`` describes the mapping only; it never marks a task successful.
    Conflicting prior controller evidence or any symlink aborts publication.
    """
    staging_root = Path(os.path.abspath(view.repo_root))
    owner_root = Path(os.path.abspath(run.repo_root))
    source = Path(os.path.abspath(view.context_handoff_dir))
    destination = Path(os.path.abspath(run.context_handoff_dir))
    if not source.is_relative_to(staging_root) or not destination.is_relative_to(owner_root):
        raise ScopeViolation('Evidence handoff must remain inside its corresponding repository')
    session_dir = Path(os.path.abspath(run.session_dir))
    source_files = _files(source)
    # Preflight all destinations before adding any handoff artifact.
    for relative, data in source_files.items():
        target = destination / relative
        if target.exists() or target.is_symlink():
            if _read(target) != data:
                raise ScopeViolation(f'Existing controller evidence differs: {target}')
        else:
            # Parent inspection must reject symlinks even for missing children.
            with _parent_fd(target, create=True): pass
    for relative, data in source_files.items():
        _write_new(destination / relative, data)

    originals = session_dir / 'evidence-reference-originals'
    def preserve(label, data):
        digest = hashlib.sha256(data).hexdigest()
        _write_new(originals / label / (digest + '.original'), data)

    # Walk securely once; reject symlinks anywhere in controller evidence state.
    for relative, data in _files(session_dir, select=lambda p: (
            p.parts[0] not in {'evidence-reference-originals', 'context_handoff'}
            and p.name.startswith('envelope-') and p.suffix == '.json')).items():
        record = json.loads(data)
        rebased = _rebase(record, staging_root, owner_root)
        if rebased != record:
            preserve('envelopes/' + relative.as_posix(), data)
            _atomic_json(session_dir / relative, rebased)

    tracer = getattr(run, 'tracer', None)
    if tracer is not None:
        conn = tracer.conn
        rows = []
        updates = []
        for table, id_column in [('events', 'event_id'), ('envelopes', 'envelope_id')]:
            for identity, raw in conn.execute(
                    f'SELECT {id_column}, payload_json FROM {table} WHERE adw_id=?', (run.adw_id,)):
                record = json.loads(raw)
                rebased = _rebase(record, staging_root, owner_root)
                if rebased != record:
                    rows.append({'table': table, 'id': identity, 'payload_json': raw})
                    updates.append((table, id_column, identity, json.dumps(rebased, ensure_ascii=False)))
        if rows:
            preserve('sqlite-payloads', json.dumps(rows, ensure_ascii=False).encode())
            conn.execute('SAVEPOINT publish_evidence')
            try:
                for table, column, identity, raw in updates:
                    conn.execute(f'UPDATE {table} SET payload_json=? WHERE {column}=? AND adw_id=?',
                                 (raw, identity, run.adw_id))
                conn.execute('RELEASE SAVEPOINT publish_evidence')
            except BaseException:
                conn.execute('ROLLBACK TO SAVEPOINT publish_evidence')
                conn.execute('RELEASE SAVEPOINT publish_evidence')
                raise
        events_path = Path(tracer.events_jsonl)
        if events_path.exists() or events_path.is_symlink():
            raw = _read(events_path)
            lines, changed = [], False
            for line in raw.splitlines(keepends=True):
                if not line.strip():
                    lines.append(line)
                    continue
                record = json.loads(line)
                rebased = _rebase(record, staging_root, owner_root) if record.get('adw_id') == run.adw_id else record
                if record != rebased:
                    changed = True
                    lines.append(json.dumps(rebased, ensure_ascii=False).encode() + b'\n')
                else:
                    lines.append(line)
            if changed:
                preserve('events-jsonl', raw)
                _replace_bytes(events_path, b''.join(lines))

    mapping_path = session_dir / 'artifact-map.json'
    mapping = {'version': 1, 'mappings': []}
    if mapping_path.exists() or mapping_path.is_symlink():
        mapping = _read_json(mapping_path)
        if not isinstance(mapping, dict) or not isinstance(mapping.get('mappings'), list):
            raise ScopeViolation('Malformed artifact mapping')
    entry = {'staging_root': str(staging_root), 'owner_root': str(owner_root),
             'status': status, 'artifact_keys': sorted(KEYS),
             'published_handoff_files': [p.as_posix() for p in sorted(source_files)],
             'original_records': 'evidence-reference-originals',
             'source_promoted': 'not established by evidence publication'}
    mapping['mappings'] = [x for x in mapping['mappings'] if x.get('staging_root') != str(staging_root)] + [entry]
    _atomic_json(mapping_path, mapping)
    return entry
