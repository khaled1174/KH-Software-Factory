"""Bind observed terminal evidence to the final, cleaned execution workspace.

A seal is controller observation, not model approval. Existing artifact, scope,
quality and review gates still judge the final files. Reads refuse symlinks and
special files and are bounded; no missing or unreadable evidence is truncated.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time

from .data_types import EnvelopeBase
from .scope import _parent_fd

MAX_ENTRIES = 10000
CAPTURE_SECONDS = 5.0


def _quoted_text_end(text, start):
    """Keep quoted text whole; never salvage data from an unfinished string."""
    escaped = False
    for position in range(start + 1, len(text)):
        char = text[position]
        if escaped:
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '"':
            return position + 1
    raise ValueError('invalid JSON framing in the response: unfinished quoted text')


def _invalid_json_constant(value):
    raise ValueError('invalid JSON constant in the response: ' + value)


def extract_json(text):
    """Decode complete objects without interpreting Markdown inside strings.

    Every role requires a top-level status. Multiple complete objects are safe
    only when exactly one has that field; other objects are ancillary metadata.
    Malformed JSON-looking fragments and competing role results fail closed.
    The caller still validates the selected object's complete output schema.
    """
    decoder = json.JSONDecoder(parse_constant=_invalid_json_constant)
    objects, position = [], 0
    while position < len(text):
        char = text[position]
        if char in '}]':
            raise ValueError('invalid JSON framing in the response: unmatched ' + char)
        if char == '"':
            position = _quoted_text_end(text, position)
            continue
        if char not in '{[':
            position += 1
            continue
        try:
            value, end = decoder.raw_decode(text, position)
        except json.JSONDecodeError as error:
            # Only a plain identifier placeholder is prose. Do not discard
            # fields, quotes, nested structures or unmatched response brackets.
            end = text.find('}' if char == '{' else ']', position + 1)
            if end != -1 and text[position + 1:end].strip().isidentifier():
                position = end + 1
                continue
            raise ValueError('invalid JSON in the response: ' + str(error)) from error
        if isinstance(value, dict):
            objects.append(value)
        # Complete strings/arrays remain whole, including any nested objects.
        position = end
    if not objects:
        raise ValueError('no JSON object found in the response')
    if len(objects) == 1:
        return objects[0]
    roles = [value for value in objects if 'status' in value]
    if len(roles) != 1:
        raise ValueError('ambiguous JSON response: expected one top-level role object')
    return roles[0]


class EvidenceSeal:
    def __init__(self, request, text):
        self.root = Path(request.cwd).resolve()
        self.limit = request.evidence_snapshot_max_bytes
        self.roots = []
        for pattern in request.evidence_paths:
            if not pattern.endswith('/**') or any(c in pattern[:-3] for c in '*?['):
                raise ValueError('Terminal evidence needs literal phase directories')
            self.roots.append(self.path(pattern[:-3]))
        self.declared = []
        try:
            envelope = EnvelopeBase.model_validate(extract_json(text))
        except ValueError:
            # Keep normal typed-JSON correction. The entire phase evidence
            # directory is sealed even when the response cannot yet be parsed.
            envelope = None
        if envelope is not None:
            declarations = {d.path: d for d in envelope.artifact_declarations}
            for name in dict.fromkeys([*envelope.artifacts, *declarations]):
                declaration = declarations.get(name)
                if declaration and declaration.base == 'evidence' and not Path(name).is_absolute():
                    if len(self.roots) != 1:
                        raise ValueError('Declared evidence base is not a single phase directory')
                    path = self.path(str(self.roots[0] / name))
                else:
                    path = self.path(name)
                self.declared.append(path)
        self.terminal = self.capture()

    def path(self, value):
        path = Path(value)
        path = Path(os.path.abspath(path if path.is_absolute() else self.root / path))
        if path == self.root or not path.is_relative_to(self.root):
            raise ValueError('Terminal artifact escapes the worker workspace')
        return path

    def capture(self):
        rows, total = {}, 0
        deadline = time.monotonic() + CAPTURE_SECONDS

        def bound():
            if len(rows) >= MAX_ENTRIES or time.monotonic() > deadline:
                raise ValueError('Terminal evidence capture exceeds its entry/time bound')

        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        def visit(parent, name, path):
            nonlocal total
            key = path.relative_to(self.root).as_posix()
            if key in rows:
                return
            bound()
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    opened = os.fstat(fd)
                    if identity(before) != identity(opened):
                        raise ValueError('Terminal evidence directory changed during capture')
                    rows[key] = {'kind': 'directory', 'mode': stat.S_IMODE(opened.st_mode)}
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            visit(fd, entry.name, path / entry.name)
                    if identity(opened) != identity(os.fstat(fd)):
                        raise ValueError('Terminal evidence directory changed during capture')
                finally:
                    os.close(fd)
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or identity(before) != identity(opened):
                        raise ValueError('Terminal evidence file changed during capture')
                    if total + opened.st_size > self.limit:
                        raise ValueError('Terminal evidence exceeds snapshot_max_bytes')
                    digest, size = hashlib.sha256(), 0
                    while True:
                        bound()
                        block = os.read(fd, 1 << 20)
                        if not block:
                            break
                        size += len(block)
                        total += len(block)
                        if total > self.limit:
                            raise ValueError('Terminal evidence exceeds snapshot_max_bytes')
                        digest.update(block)
                    if identity(opened) != identity(os.fstat(fd)) or size != opened.st_size:
                        raise ValueError('Terminal evidence file changed during capture')
                    rows[key] = {'kind': 'file', 'mode': stat.S_IMODE(opened.st_mode),
                                 'bytes': size, 'sha256': digest.hexdigest()}
                finally:
                    os.close(fd)
            else:
                raise ValueError('Terminal evidence must use regular files and directories')
            if identity(before) != identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                raise ValueError('Terminal evidence path changed during capture')

        for path in dict.fromkeys([*self.roots, *self.declared]):
            with _parent_fd(path) as (parent, name):
                visit(parent, name, path)
        for path in self.declared:
            if rows[path.relative_to(self.root).as_posix()]['kind'] != 'file':
                raise ValueError('Declared artifact is not a regular file')
        return rows

    @staticmethod
    def digest(rows):
        return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def verify(self):
        final = self.capture()
        changed = sorted(path for path in set(self.terminal) | set(final)
                         if self.terminal.get(path) != final.get(path))
        return {'verified': not changed, 'changed_paths': changed,
                'terminal_sha256': self.digest(self.terminal), 'final_sha256': self.digest(final),
                'terminal_files': self.terminal, 'final_files': final}
