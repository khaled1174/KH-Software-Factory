"""Read an owner-accepted intent and bind it to the exact task request.

Acceptance is also bound to the content it accepted, without asking the owner
for anything beyond `Accepted by` and `Accepted on`. The first guarded run that
uses an accepted intent records the SHA-256 of its text in
`.kh/intent-digests.json` (trust on first use); every later run compares
against it, so an edit after acceptance, including to the sensitive-change
approvals, voids the acceptance until the owner re-accepts (new `Accepted on`
or `Accepted by`, or an explicit `Accepted digest` line). An explicit
`Accepted digest: <sha256 of the text without that line>` under
`## Acceptance` pins the content from the start and takes precedence over the
record. Previews, planning and dry runs read the record but never write it.
This catches drift; it is not an identity check, because an agent running as
the owner's OS user can write the same line or record.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .scope import ScopeViolation, _atomic_json, _parent_fd, _read_json


MAX_INTENT_BYTES = 16384
DIGEST_LINE = re.compile(r'(?m)^Accepted digest:[^\n]*(?:\n|\Z)')
CATEGORY_NAME = re.compile(r'[a-z][a-z0-9_-]{0,39}')
LEDGER = '.kh/intent-digests.json'
LEDGER_LOCK = '.kh/intent-digests.lock'
LEDGER_SCHEMA = 1


class IntentBlocked(ValueError):
    """No valid owner-accepted intent authorizes this planning request."""


def content_digest(content: str) -> str:
    """SHA-256 of the intent text without its `Accepted digest` line."""
    return hashlib.sha256(DIGEST_LINE.sub('', content, count=1).encode('utf-8')).hexdigest()


def _approved_sensitive(acceptance: str) -> list[str]:
    """Owner-approved sensitive-change categories; absent or `none` approves nothing."""
    values = re.findall(r'(?m)^Approved sensitive changes:[ \t]*(.*?)[ \t]*$', acceptance)
    if len(values) > 1:
        raise IntentBlocked('Intent must have at most one Approved sensitive changes field')
    if not values or values[0].lower() in ('', 'none'):
        return []
    names = [part.strip().lower() for part in values[0].split(',')]
    if any(not CATEGORY_NAME.fullmatch(name) for name in names):
        raise IntentBlocked('Approved sensitive changes must be "none" or comma-separated category names')
    return sorted(set(names))


def _section(source: str, heading: str) -> str:
    match = re.search(r'(?ms)^## ' + re.escape(heading) + r'[ \t]*\n(.*?)(?=^## |\Z)', source)
    return match.group(1).strip() if match else ''


def _field(source: str, name: str) -> str:
    matches = re.findall(r'(?m)^' + re.escape(name) + r':[ \t]*(.*?)[ \t]*$', source)
    if len(matches) != 1:
        raise IntentBlocked(f'Intent must have exactly one {name} field')
    return matches[0].strip()


def _read(path: Path) -> str:
    with _parent_fd(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_INTENT_BYTES:
                raise IntentBlocked(f'Intent is not a bounded regular file: {path}')
            return stream.read(MAX_INTENT_BYTES + 1).decode('utf-8')


def load(root: Path, request: str, *, allow_draft=False) -> dict:
    """Require one matching intent under intents/, with an explicit owner decision."""
    root = Path(root).resolve()
    directory = root / 'intents'
    if directory.is_symlink() or not directory.is_dir():
        raise IntentBlocked('Create an intents/ directory with an accepted intent for this task')
    matches = []
    for path in sorted(directory.glob('*.md')):
        try:
            content = _read(path)
        except (OSError, ScopeViolation) as error:
            raise IntentBlocked(f'Cannot safely read intent: {path}') from error
        if _section(content, 'Task request') == request.strip():
            matches.append((path, content))
    if len(matches) != 1:
        raise IntentBlocked('Intent missing or ambiguous: expected exactly one matching Task request')
    path, content = matches[0]
    status = _field(content, 'Status')
    if status not in ('draft', 'accepted'):
        raise IntentBlocked('Intent Status must be draft or accepted')
    if status != 'accepted' and not allow_draft:
        raise IntentBlocked('Intent for this task is a draft; owner acceptance is required')
    for heading in ('Problem', 'Proposed outcome'):
        value = _section(content, heading)
        if not value or value.startswith('<'):
            raise IntentBlocked(f'Intent {heading} must be completed')
    accepted_by = accepted_on = accepted_digest = binding = None
    approved_sensitive = []
    if status == 'accepted':
        acceptance = _section(content, 'Acceptance')
        accepted_by = _field(acceptance, 'Accepted by')
        accepted_on = _field(acceptance, 'Accepted on')
        if not accepted_by or accepted_by.startswith('<'):
            raise IntentBlocked('Intent must name the accepting owner')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', accepted_on):
            raise IntentBlocked('Intent acceptance date must use YYYY-MM-DD')
        try:
            date.fromisoformat(accepted_on)
        except ValueError as error:
            raise IntentBlocked('Intent acceptance date is invalid') from error
        approved_sensitive = _approved_sensitive(acceptance)
        current = content_digest(content)
        lines = DIGEST_LINE.findall(content)
        recorded = re.findall(r'(?m)^Accepted digest:[ \t]*(.*?)[ \t]*$', acceptance)
        relative = path.relative_to(root).as_posix()
        if lines or recorded:
            if len(lines) != 1 or len(recorded) != 1:
                raise IntentBlocked(
                    f'Intent acceptance is not bound to its content: {path.name} needs at most one '
                    f'"Accepted digest" line, under ## Acceptance (its current value: {current})')
            accepted_digest, binding = recorded[0], 'explicit'
        else:
            entry = _ledger(root).get(relative)
            if entry and (entry.get('accepted_by'), entry.get('accepted_on')) == (accepted_by, accepted_on):
                accepted_digest, binding = entry.get('digest'), 'recorded'
            else:
                accepted_digest, binding = current, 'unbound'
        if accepted_digest != current:
            raise IntentBlocked(_changed(path.name, accepted_digest, current))
    return {'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(content.encode()).hexdigest(),
            'status': status, 'accepted_by': accepted_by, 'accepted_on': accepted_on,
            'accepted_digest': accepted_digest, 'acceptance_binding': binding,
            'approved_sensitive': approved_sensitive, 'content': content}


def _changed(name, accepted, current) -> str:
    return (f'Intent changed after acceptance ({name}: accepted {str(accepted)[:12]}, now {current[:12]}). '
            f'Review the change, then re-accept: update "Accepted on" (or "Accepted by"), or put '
            f'"Accepted digest: {current}" under ## Acceptance')


def _ledger(root: Path) -> dict:
    """Recorded first-use acceptances by intent path; an unreadable record fails closed."""
    try:
        value = _read_json(root / LEDGER)
    except FileNotFoundError:
        return {}
    except ScopeViolation as error:
        if isinstance(error.__cause__, FileNotFoundError):  # no .kh/ yet
            return {}
        raise IntentBlocked(f'Intent digest record {LEDGER} is unreadable; restore it, or delete it '
                            f'to re-bind every accepted intent on its next run') from error
    except (OSError, ValueError) as error:
        raise IntentBlocked(f'Intent digest record {LEDGER} is unreadable; restore it, or delete it '
                            f'to re-bind every accepted intent on its next run') from error
    if (not isinstance(value, dict) or value.get('schema_version') != LEDGER_SCHEMA
            or not isinstance(value.get('intents'), dict)):
        raise IntentBlocked(f'Intent digest record {LEDGER} has an unknown format')
    return value['intents']


def bind(root: Path, record: dict) -> dict:
    """Record an unbound acceptance on its first guarded run (trust on first use).

    Only the guarded task controller calls this, after every preflight check
    passed; it binds exactly the text that was loaded and handed to the agents.
    """
    if record.get('acceptance_binding') != 'unbound':
        return record
    root = Path(root).resolve()
    with _parent_fd(root / LEDGER_LOCK, create=True) as (parent, name):
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'a+') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise IntentBlocked(f'Refusing non-regular {LEDGER_LOCK}')
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            entries = dict(_ledger(root))
            entry = entries.get(record['path'])
            if (entry and (entry.get('accepted_by'), entry.get('accepted_on'))
                    == (record['accepted_by'], record['accepted_on'])
                    and entry.get('digest') != record['accepted_digest']):
                raise IntentBlocked(_changed(Path(record['path']).name, entry.get('digest'),
                                             record['accepted_digest']))
            entries[record['path']] = {
                'digest': record['accepted_digest'], 'accepted_by': record['accepted_by'],
                'accepted_on': record['accepted_on'],
                'bound_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            _atomic_json(root / LEDGER, {'schema_version': LEDGER_SCHEMA, 'intents': entries})
    return {**record, 'acceptance_binding': 'first-use'}


def attach(context_packet: dict, record: dict) -> dict:
    """Include the accepted intent in the reviewed, size-bounded context."""
    extra = len(json.dumps(record, ensure_ascii=False).encode('utf-8'))
    budget = context_packet['budget']
    used = budget['used_bytes'] + extra
    if used > budget['limit_bytes']:
        raise IntentBlocked('Accepted intent exceeds the remaining project context budget')
    return {**context_packet, 'intent': record,
            'budget': {**budget, 'used_bytes': used}}
