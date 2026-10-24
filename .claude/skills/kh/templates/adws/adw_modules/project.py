"""Controller-owned project ledger and derived reports.

The SQLite database is authoritative. Reports are rebuildable projections. These
APIs coordinate trusted KH controllers; they are not an OS security boundary
against arbitrary code running with the owner's filesystem privileges.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import warnings

from .scope import ScopeContract, ScopeViolation, _atomic_json, _parent_fd, _read_json, _relative_pattern, plan_boundary

SCHEMA_VERSION = 1
ACTIVE = frozenset({'running', 'verifying', 'reviewing', 'needs_repair'})
TRANSITIONS = {
    'running': {'verifying', 'needs_repair'},
    'verifying': {'reviewing', 'needs_repair'},
    'reviewing': {'needs_repair'},
    'needs_repair': {'running'},
}
ID_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')


class ProjectError(ValueError):
    """A plan, ledger operation, or controller receipt was rejected."""


def require_sequential(root):
    """Refuse historical parallel authority without changing or importing it."""
    for relative in ('.kh/parallel', '.kh/execution-decision.json', '.kh/execution-metadata.json'):
        path = Path(root) / relative
        if path.exists() or path.is_symlink():
            raise ProjectError('LEGACY_PARALLEL_STATE: ' + relative +
                ' is no longer supported. Preserve this state and reconcile it with the prior release; '
                'it cannot authorize sequential execution.')


def _id(value, label='ID'):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ProjectError(f'{label} must use 1-80 letters, digits, underscores or hyphens')
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ProjectError(f'{label} must be a nonempty string without NUL')
    return value


def _object(value, required, optional, label):
    if not isinstance(value, dict):
        raise ProjectError(f'{label} must be an object')
    missing, unknown = required - value.keys(), value.keys() - required - optional
    if missing or unknown:
        raise ProjectError(f'{label}: missing fields {sorted(missing)}; unknown fields {sorted(unknown)}')


def validate_plan(plan: dict) -> dict:
    """Validate the complete plan before creating any filesystem state."""
    _object(plan, {'schema_version', 'project', 'milestones', 'tasks'}, set(), 'Plan')
    if type(plan['schema_version']) is not int or plan['schema_version'] != SCHEMA_VERSION:
        raise ProjectError('Unsupported plan schema_version; expected 1')
    _object(plan['project'], {'id', 'name'}, set(), 'Project')
    project = {'id': _id(plan['project']['id'], 'Project ID'),
               'name': _text(plan['project']['name'], 'Project name')}
    if not isinstance(plan['milestones'], list) or not plan['milestones']:
        raise ProjectError('Plan must contain at least one milestone')
    milestones, milestone_ids = [], set()
    for item in plan['milestones']:
        _object(item, {'id', 'title'}, set(), 'Milestone')
        ident = _id(item['id'], 'Milestone ID')
        if ident in milestone_ids:
            raise ProjectError(f'Duplicate milestone ID: {ident}')
        milestone_ids.add(ident)
        milestones.append({'id': ident, 'title': _text(item['title'], 'Milestone title')})
    if not isinstance(plan['tasks'], list) or not plan['tasks']:
        raise ProjectError('Plan must contain at least one task')
    tasks, task_ids = [], set()
    required = {'id', 'milestone_id', 'title', 'request', 'deliverables', 'allowed_paths'}
    optional = {'dependencies', 'constraints', 'non_goals', 'required_checks', 'weight'}
    for item in plan['tasks']:
        _object(item, required, optional, 'Task')
        ident = _id(item['id'], 'Task ID')
        if ident in task_ids:
            raise ProjectError(f'Duplicate task ID: {ident}')
        task_ids.add(ident)
        milestone_id = _id(item['milestone_id'], 'Milestone ID')
        if milestone_id not in milestone_ids:
            raise ProjectError(f'Task {ident} references unknown milestone {milestone_id}')
        title = _text(item['title'], 'Task title')
        try:
            contract = ScopeContract(original_request=item['request'], deliverables=item['deliverables'],
                allowed_paths=item['allowed_paths'], constraints=item.get('constraints', []),
                non_goals=item.get('non_goals', []), required_checks=item.get('required_checks', []))
        except (ValueError, TypeError) as exc:
            raise ProjectError(f'Invalid scope for task {ident}: {exc}') from exc
        if not contract.allowed_paths:
            raise ProjectError(f'Task {ident} must declare nonempty allowed_paths')
        dependencies = item.get('dependencies', [])
        if not isinstance(dependencies, list):
            raise ProjectError(f'Task {ident} dependencies must be a list')
        dependencies = [_id(dep, 'Dependency ID') for dep in dependencies]
        if len(set(dependencies)) != len(dependencies):
            raise ProjectError(f'Task {ident} contains duplicate dependencies')
        weight = item.get('weight', 1)
        if type(weight) not in (int, float) or not math.isfinite(weight) or weight <= 0:
            raise ProjectError(f'Task {ident} weight must be a finite positive number')
        tasks.append({'id': ident, 'milestone_id': milestone_id, 'title': title,
            'request': contract.original_request, 'deliverables': contract.deliverables,
            'allowed_paths': contract.allowed_paths, 'dependencies': dependencies,
            'constraints': contract.constraints, 'non_goals': contract.non_goals,
            'required_checks': contract.required_checks, 'weight': weight})
    by_id = {task['id']: task for task in tasks}
    for task in tasks:
        unknown = set(task['dependencies']) - task_ids
        if unknown:
            raise ProjectError(f"Task {task['id']} references unknown dependencies: {sorted(unknown)}")
    # Kahn's algorithm avoids recursion limits on real, long project plans.
    indegree = {task['id']: len(task['dependencies']) for task in tasks}
    children = {ident: [] for ident in task_ids}
    for task in tasks:
        for dep in task['dependencies']:
            children[dep].append(task['id'])
    ready = [ident for ident, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        ident = ready.pop()
        visited += 1
        for child in children[ident]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(by_id):
        raise ProjectError('Task dependencies contain a cycle')
    return {'schema_version': SCHEMA_VERSION, 'project': project, 'milestones': milestones, 'tasks': tasks}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _root(root):
    path = Path(os.path.abspath(root))
    if not path.is_dir():
        raise ProjectError(f'Project root is not a directory: {path}')
    return path


def _regular_at(parent, name, *, missing=False):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if missing:
            return
        raise ProjectError(f'Project ledger not initialized: {name}')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ProjectError(f'Refusing non-regular or hard-linked project state: {name}')


@contextmanager
def _db(root, *, write=False):
    path = _root(root) / '.kh/project.db'
    try:
        with _parent_fd(path) as (parent, name):
            _regular_at(parent, name)
            for suffix in ('-journal', '-wal', '-shm'):
                _regular_at(parent, name + suffix, missing=True)
            conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                                   uri=True, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute('PRAGMA foreign_keys=ON')
                version = conn.execute('PRAGMA user_version').fetchone()[0]
                if version != SCHEMA_VERSION:
                    raise ProjectError(f'Unsupported project database schema: {version}')
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
    except (sqlite3.Error, ScopeViolation, OSError) as exc:
        raise ProjectError(f'Cannot access project ledger: {exc}') from exc


def _event(conn, task_id, run_id, event, detail=None):
    conn.execute('INSERT INTO events(task_id,run_id,event,detail,created_at) VALUES(?,?,?,?,?)',
                 (task_id, run_id, event, json.dumps(detail, ensure_ascii=False), _now()))


def _row(conn, task_id):
    _id(task_id, 'Task ID')
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise ProjectError(f'Unknown task ID: {task_id}')
    return row


def _task(row):
    result = json.loads(row['definition'])
    result.update({key: row[key] for key in ('status', 'run_id', 'summary', 'blocker', 'updated_at')})
    for key in ('changed_files', 'evidence', 'receipt', 'detail'):
        result[key] = json.loads(row[key])
    return result


def _snapshot(conn):
    metadata = conn.execute('SELECT plan FROM project WHERE singleton=1').fetchone()
    if metadata is None:
        raise ProjectError('Project ledger contains no project')
    plan = json.loads(metadata[0])
    tasks = [_task(row) for row in conn.execute('SELECT * FROM tasks ORDER BY position')]
    snapshot = _summarize(plan, tasks)
    from .methodology_metrics import metrics
    histories = {}
    for row in conn.execute('SELECT task_id,run_id,event,detail FROM events ORDER BY sequence'):
        histories.setdefault(row['task_id'], []).append(dict(row))
    snapshot['methodology_metrics'] = metrics(snapshot, histories)
    return snapshot


def _percent(tasks):
    if not tasks:
        return 0.0
    # Scaling prevents finite individual weights from overflowing their sum.
    scale = max(task['weight'] for task in tasks)
    total = math.fsum(task['weight'] / scale for task in tasks)
    accepted = math.fsum(task['weight'] / scale for task in tasks if task['status'] == 'completed')
    return round(100 * accepted / total, 2)


def _summarize(plan, tasks):
    completed = {task['id'] for task in tasks if task['status'] == 'completed'}
    milestones = []
    for item in plan['milestones']:
        group = [task for task in tasks if task['milestone_id'] == item['id']]
        done = sum(task['status'] == 'completed' for task in group)
        state = ('empty' if not group else 'completed' if done == len(group) else
                 'in_progress' if any(task['status'] in ACTIVE for task in group) else
                 'blocked' if any(task['status'] == 'blocked' for task in group) else
                 'in_progress' if done else 'pending')
        milestones.append({**item, 'total': len(group), 'completed': done, 'status': state,
                           'completion_percent': _percent(group)})
    totals = {'tasks': len(tasks), 'completed': len(completed),
        'pending': sum(task['status'] == 'pending' for task in tasks),
        'active': sum(task['status'] in ACTIVE for task in tasks),
        'blocked': sum(task['status'] == 'blocked' for task in tasks),
        'completion_percent': _percent(tasks)}
    ready = [task['id'] for task in tasks if task['status'] == 'pending'
             and set(task['dependencies']) <= completed]
    return {'schema_version': SCHEMA_VERSION, 'project': plan['project'], 'totals': totals,
            'milestones': milestones, 'tasks': tasks, 'next_tasks': ready}


def status(root: Path) -> dict:
    """Read the current committed snapshot without creating or editing files."""
    with _db(root) as conn:
        return _snapshot(conn)


def export_plan(root: Path) -> dict:
    """Read the current canonical definitions, without derived task state."""
    with _db(root) as conn:
        return validate_plan(json.loads(conn.execute(
            'SELECT plan FROM project WHERE singleton=1').fetchone()[0]))


def load_task(root: Path, task_id: str) -> dict:
    with _db(root) as conn:
        return _task(_row(conn, task_id))


def _refresh_after_commit(root, task_id=None):
    try:
        snapshot = refresh_reports(root)
    except (ProjectError, ScopeViolation, OSError) as exc:
        message = f'Project state committed; report refresh failed. Run project report to rebuild: {exc}'
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        snapshot = status(root)
        snapshot['report_error'] = message
    if task_id is None:
        return snapshot
    task = next(task for task in snapshot['tasks'] if task['id'] == task_id)
    if 'report_error' in snapshot:
        task = {**task, 'report_error': snapshot['report_error']}
    return task


def init_project(root: Path, plan: dict, *, dry_run=False) -> dict:
    """Import a complete validated plan once; dry-run writes nothing."""
    plan = validate_plan(plan)
    root = _root(root)
    path = root / '.kh/project.db'
    if path.exists() or path.is_symlink():
        raise ProjectError('Project is already initialized; existing plans are never overwritten')
    if path.parent.is_symlink():
        raise ProjectError('Refusing symlinked project state directory')
    if dry_run:
        tasks = [{**task, 'status': 'pending', 'run_id': None, 'summary': '', 'changed_files': [],
                  'evidence': [], 'blocker': None, 'receipt': None, 'detail': None,
                  'updated_at': None} for task in plan['tasks']]
        return {**_summarize(plan, tasks), 'dry_run': True}
    from .session import acquire_repo_lock
    with acquire_repo_lock(root):
        try:
            with _parent_fd(path, create=True) as (parent, name):
                descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
                os.close(descriptor)
                for suffix in ('-journal', '-wal', '-shm'):
                    if os.path.lexists(path.with_name(name + suffix)):
                        os.unlink(name, dir_fd=parent)
                        raise ProjectError('Refusing existing SQLite sidecar before project initialization')
                conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
                try:
                    conn.executescript('''
                        PRAGMA foreign_keys=ON;
                        BEGIN IMMEDIATE;
                        CREATE TABLE project(singleton INTEGER PRIMARY KEY CHECK(singleton=1), plan TEXT NOT NULL);
                        CREATE TABLE tasks(id TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE,
                            definition TEXT NOT NULL, status TEXT NOT NULL, run_id TEXT, summary TEXT NOT NULL DEFAULT '',
                            changed_files TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '[]',
                            blocker TEXT, receipt TEXT NOT NULL DEFAULT 'null', detail TEXT NOT NULL DEFAULT 'null',
                            updated_at TEXT NOT NULL);
                        CREATE TABLE attempts(run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id));
                        CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT, event TEXT NOT NULL,
                            detail TEXT NOT NULL, created_at TEXT NOT NULL);
                    ''')
                    conn.execute('INSERT INTO project VALUES(1,?)', (json.dumps(plan, ensure_ascii=False),))
                    for index, task in enumerate(plan['tasks']):
                        conn.execute('INSERT INTO tasks(id,position,definition,status,updated_at) VALUES(?,?,?,?,?)',
                                     (task['id'], index, json.dumps(task, ensure_ascii=False), 'pending', _now()))
                        _event(conn, task['id'], None, 'pending', 'Imported approved project plan')
                    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    conn.close()
                    os.unlink(name, dir_fd=parent)
                    raise
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, ScopeViolation) as exc:
            raise ProjectError(f'Cannot initialize project ledger: {exc}') from exc
        return _refresh_after_commit(root)


def amend_task(root: Path, task_id: str, definition: dict, *, reason: str, dry_run=False) -> dict:
    """Owner replaces a pending/blocked definition with an accepted-intent request.

    The task ID and historical records survive. The amended task is pending;
    its previous runtime state is retained in the append-only revision event.
    Dry-run validates and returns the proposed snapshot without writing files.
    """
    return _revise_tasks(root, task_id, [definition], reason=reason, dry_run=dry_run, split=False)


def split_task(root: Path, task_id: str, replacements: list[dict], *, reason: str, dry_run=False) -> dict:
    """Owner splits an unaccepted task; the first replacement retains its ID.

    Children inherit its milestone, prerequisites, constraints, non-goals and
    checks; their scopes may only narrow its allowed paths. Their weights must
    total the original weight. Dependents wait for every terminal replacement.
    Candidates and old approval/receipt files are never moved or changed here.
    """
    if not isinstance(replacements, list) or len(replacements) < 2:
        raise ProjectError('A task split requires a JSON list of at least two replacement task definitions')
    return _revise_tasks(root, task_id, replacements, reason=reason, dry_run=dry_run, split=True)


def _revise_tasks(root, task_id, replacements, *, reason, dry_run, split):
    _text(reason, 'Revision reason')
    _id(task_id, 'Task ID')
    if not isinstance(replacements[0], dict) or replacements[0].get('id') != task_id:
        raise ProjectError('The first replacement must retain the original task ID')
    root = _root(root)
    from . import intent
    from .session import acquire_repo_lock
    # A preview must not create a lock file or any other filesystem state.
    with nullcontext() if dry_run else acquire_repo_lock(root):
        with _db(root, write=not dry_run) as conn:
            row = _row(conn, task_id)
            if row['status'] not in {'pending', 'blocked'}:
                raise ProjectError(f"Only pending or blocked tasks may be revised; {task_id} is {row['status']}")
            if any(item['status'] in ACTIVE for item in conn.execute('SELECT status FROM tasks')):
                raise ProjectError('Another project task is active; finish or recover it before revising the plan')
            rows = list(conn.execute('SELECT * FROM tasks ORDER BY position'))
            states = {item['id']: _task(item) for item in rows}
            original = json.loads(row['definition'])
            metadata = conn.execute('SELECT plan FROM project WHERE singleton=1').fetchone()
            if metadata is None:
                raise ProjectError('Project ledger contains no project')
            plan = json.loads(metadata[0])
            current = [json.loads(item['definition']) for item in rows]
            index = next(i for i, item in enumerate(current) if item['id'] == task_id)
            proposed = validate_plan({**plan, 'tasks': [*current[:index], *replacements, *current[index + 1:]]})
            children = proposed['tasks'][index:index + len(replacements)]
            if split:
                if any(child['milestone_id'] != original['milestone_id'] for child in children):
                    raise ProjectError('Split replacements must retain the original milestone')
                # Compare the JSON numeric values exactly, including decimal weights,
                # without float summation drift or overflow of large finite weights.
                if sum(Fraction(str(child['weight'])) for child in children) != Fraction(str(original['weight'])):
                    raise ProjectError('Split replacement weights must total the original task weight')
                contract = ScopeContract(original_request=original['request'],
                    deliverables=original['deliverables'], allowed_paths=original['allowed_paths'])
                for child in children:
                    try:
                        plan_boundary(contract, child['allowed_paths'])
                    except ScopeViolation as exc:
                        raise ProjectError(f'Split task {child["id"]} exceeds the original owner scope: {exc}') from exc
                    for key in ('dependencies', 'constraints', 'non_goals', 'required_checks'):
                        child[key] = list(dict.fromkeys([*original[key], *child[key]]))
                internal_prerequisites = {dep for child in children for dep in child['dependencies']}
                terminals = [child['id'] for child in children if child['id'] not in internal_prerequisites]
                child_ids = {child['id'] for child in children}
                for task in proposed['tasks']:
                    if task['id'] in child_ids or task_id not in task['dependencies']:
                        continue
                    if states[task['id']]['status'] == 'completed':
                        raise ProjectError(f'Cannot rewrite dependencies of completed task {task["id"]}')
                    task['dependencies'] = list(dict.fromkeys(
                        dep for old in task['dependencies'] for dep in (terminals if old == task_id else [old])))
                proposed = validate_plan(proposed)
                children = proposed['tasks'][index:index + len(replacements)]
            acceptances = []
            for child in children:
                try:
                    record = intent.load(root, child['request'], allow_draft=False)
                except intent.IntentBlocked as exc:
                    raise ProjectError(f'Task {child["id"]} requires an accepted matching intent: {exc}') from exc
                acceptances.append({key: record[key] for key in ('path', 'sha256', 'accepted_by', 'accepted_on')})
            saved_candidate = os.path.lexists(root / '.kh/work' / task_id / 'current')
            revision = {'operation': 'split' if split else 'amend', 'task_id': task_id,
                        'task_ids': [child['id'] for child in children], 'stale_candidate': saved_candidate,
                        'candidate_message': ('Saved candidate retained; the revised task requires normal resume '
                                              'validation before reuse.' if saved_candidate else None)}
            if dry_run:
                preview = []
                for definition in proposed['tasks']:
                    if definition['id'] in revision['task_ids']:
                        state = {'status': 'pending', 'run_id': None, 'summary': '', 'changed_files': [],
                                 'evidence': [], 'blocker': None, 'receipt': None, 'detail': None, 'updated_at': None}
                    else:
                        state = {key: value for key, value in states[definition['id']].items() if key not in definition}
                    preview.append({**definition, **state})
                return {**_summarize(proposed, preview), 'dry_run': True, 'revision': revision}
            now = _now()
            # Shift later positions from the end so the UNIQUE constraint stays
            # true throughout the transaction while inserting adjacent children.
            if split:
                for item in reversed(rows[index + 1:]):
                    conn.execute('UPDATE tasks SET position=? WHERE id=?',
                                 (item['position'] + len(children) - 1, item['id']))
            for offset, child in enumerate(children):
                encoded = json.dumps(child, ensure_ascii=False, allow_nan=False)
                if offset == 0:
                    conn.execute("UPDATE tasks SET definition=?,status='pending',run_id=NULL,summary='',"
                                 "changed_files='[]',evidence='[]',blocker=NULL,receipt='null',detail='null',updated_at=? WHERE id=?",
                                 (encoded, now, task_id))
                else:
                    conn.execute('INSERT INTO tasks(id,position,definition,status,updated_at) VALUES(?,?,?,?,?)',
                                 (child['id'], row['position'] + offset, encoded, 'pending', now))
            _event(conn, task_id, None, 'split' if split else 'amended',
                   {'reason': reason, 'before': original, 'after': children if split else children[0],
                    'previous_state': states[task_id], 'accepted_intents': acceptances, **revision})
            for child in children[1:]:
                _event(conn, child['id'], None, 'split_created',
                       {'reason': reason, 'source_task_id': task_id, 'before': None, 'after': child})
            for definition in proposed['tasks']:
                if definition['id'] in revision['task_ids']:
                    continue
                previous = next(item for item in current if item['id'] == definition['id'])
                if previous != definition:
                    conn.execute('UPDATE tasks SET definition=?,updated_at=? WHERE id=?',
                                 (json.dumps(definition, ensure_ascii=False, allow_nan=False), now, definition['id']))
                    _event(conn, definition['id'], None, 'dependencies_rewired',
                           {'reason': reason, 'source_task_id': task_id, 'before': previous, 'after': definition})
            conn.execute('UPDATE project SET plan=? WHERE singleton=1',
                         (json.dumps(proposed, ensure_ascii=False, allow_nan=False),))
        return {**_refresh_after_commit(root), 'revision': revision}


def begin_task(root: Path, task_id: str, run_id: str, *, require_plan_review=False) -> dict:
    """Controller starts a ready task while holding the repository lock."""
    _id(run_id, 'Run ID')
    with _db(root, write=True) as conn:
        row = _row(conn, task_id)
        if row['status'] not in {'pending', 'blocked'}:
            raise ProjectError(f"Task {task_id} cannot start from {row['status']}")
        definition = json.loads(row['definition'])
        unmet = [dep for dep in definition['dependencies'] if _row(conn, dep)['status'] != 'completed']
        if unmet:
            raise ProjectError(f'Task {task_id} has incomplete dependencies: {unmet}')
        if conn.execute('SELECT 1 FROM tasks WHERE status IN (?,?,?,?) LIMIT 1', tuple(sorted(ACTIVE))).fetchone():
            raise ProjectError('Another project task is active; finish or recover it before starting a task')
        if conn.execute('SELECT 1 FROM attempts WHERE run_id=?', (run_id,)).fetchone():
            raise ProjectError('Run ID has already been used; start a new controller run')
        conn.execute('INSERT INTO attempts VALUES(?,?)', (run_id, task_id))
        conn.execute("UPDATE tasks SET status='running',run_id=?,blocker=NULL,summary='',changed_files='[]',"
                     "evidence='[]',receipt='null',detail='null',updated_at=? WHERE id=?",
                     (run_id, _now(), task_id))
        _event(conn, task_id, run_id, 'running', {'requires_plan_review': True} if require_plan_review else None)
    return _refresh_after_commit(root, task_id)


def _owned(row, run_id):
    _id(run_id, 'Run ID')
    if row['run_id'] != run_id:
        raise ProjectError('Stale controller run cannot update this task')


def transition(root: Path, task_id: str, run_id: str, status: str, detail=None) -> dict:
    """Controller-only intermediate transition; never accepts completion."""
    encoded = json.dumps(detail, ensure_ascii=False, allow_nan=False)
    with _db(root, write=True) as conn:
        row = _row(conn, task_id)
        _owned(row, run_id)
        if status not in TRANSITIONS.get(row['status'], set()):
            raise ProjectError(f"Invalid task transition: {row['status']} -> {status}")
        changed = conn.execute('UPDATE tasks SET status=?,detail=?,updated_at=? WHERE id=? AND run_id=? AND status=?',
            (status, encoded, _now(), task_id, run_id, row['status'])).rowcount
        if changed != 1:
            raise ProjectError('Task changed during transition')
        _event(conn, task_id, run_id, status, detail)
    return _refresh_after_commit(root, task_id)


def record_handoff(root: Path, task_id: str, run_id: str, handoff: dict) -> dict:
    """Persist each agent's return immediately, without changing task acceptance.

    Artifact paths are controller-normalized repository-relative references.
    Files may still be staged at this point; completion validates published
    evidence later. Empty agent summaries are allowed and remain visibly empty.
    """
    _object(handoff, {'phase', 'status', 'summary'}, {'artifacts', 'gates'}, 'Agent handoff')
    _text(handoff['phase'], 'Handoff phase')
    _text(handoff['status'], 'Handoff status')
    if not isinstance(handoff['summary'], str) or '\x00' in handoff['summary']:
        raise ProjectError('Handoff summary must be a string without NUL')
    artifacts = handoff.get('artifacts', [])
    if not isinstance(artifacts, list) or any(not isinstance(path, str) for path in artifacts):
        raise ProjectError('Handoff artifacts must be a list of relative file paths')
    try:
        for path in artifacts:
            _relative_pattern(path)
            if '*' in path or '?' in path or path.endswith('/'):
                raise ProjectError('Handoff artifacts must name files, not path patterns')
    except ValueError as exc:
        raise ProjectError(str(exc)) from exc
    gates = handoff.get('gates', [])
    if not isinstance(gates, list) or any(not isinstance(item, str) for item in gates):
        raise ProjectError('Handoff gates must be a list of strings')
    handoff = {**handoff, 'artifacts': list(artifacts)}
    with _db(root, write=True) as conn:
        row = _row(conn, task_id)
        _owned(row, run_id)
        if row['status'] not in ACTIVE:
            raise ProjectError('Agent handoffs require an active task owned by the controller')
        detail = json.loads(row['detail'])
        if detail is None:
            detail = {}
        elif not isinstance(detail, dict):
            detail = {'previous_detail': detail}
        history = detail.get('handoffs', [])
        if not isinstance(history, list):
            raise ProjectError('Existing handoff detail is not a list')
        detail['handoffs'] = [*history, handoff]
        changed = conn.execute('UPDATE tasks SET detail=?,updated_at=? WHERE id=? AND run_id=? AND status=?',
            (json.dumps(detail, ensure_ascii=False, allow_nan=False), _now(), task_id, run_id, row['status'])).rowcount
        if changed != 1:
            raise ProjectError('Task changed during handoff recording')
        _event(conn, task_id, run_id, 'handoff', handoff)
    return _refresh_after_commit(root, task_id)


def receipt_path(root: Path, task_id: str, run_id: str) -> Path:
    return _root(root) / '.kh/task-receipts' / _id(task_id, 'Task ID') / (_id(run_id, 'Run ID') + '.json')


def _file_evidence(root, value):
    if not isinstance(value, str):
        raise ProjectError('Evidence paths must be strings')
    try:
        _relative_pattern(value)
        if '*' in value or '?' in value or value.endswith('/'):
            raise ProjectError('Evidence must name a file, not a path pattern')
        with _parent_fd(_root(root) / value) as (parent, name):
            _regular_at(parent, name)
    except (ScopeViolation, OSError, ValueError) as exc:
        raise ProjectError(f'Invalid evidence path {value!r}: {exc}') from exc
    return value


def _validate_documentation_receipt(root, receipt, approved_plan, *, live_publication=True):
    impact = approved_plan.get('documentation') or {}
    if impact.get('decision') != 'required':
        return
    from . import project_context as pc
    docs = receipt.get('documentation')
    if (not isinstance(docs, dict) or docs.get('status') != 'published' or docs.get('decision') != 'required'
            or docs.get('checks_passed') is not True or docs.get('review_approved') is not True
            or docs.get('candidate_sha256') != receipt.get('source_sha256')
            or docs.get('run_id') != receipt.get('run_id')
            or sorted(docs.get('paths') or []) != sorted(impact['paths'])):
        raise ProjectError('Required documentation lacks a matching checked, reviewed publication receipt')
    path = _file_evidence(root, receipt.get('documentation_path'))
    if _read_json(_root(root) / path) != docs:
        raise ProjectError('Saved documentation receipt differs')
    rows = docs.get('documents') or []
    if len(rows) != len(impact['paths']) or {row['path'] for row in rows} != set(impact['paths']):
        raise ProjectError('Required documentation coverage differs')
    # Accepted historical proof remains bound to its immutable saved receipt.
    # Later reviewed tasks/refreshes may legitimately revise the live guide/index.
    if not live_publication:
        return
    for row in rows:
        path = _file_evidence(root, row['path'])
        if pc.digest(pc._read(_root(root), path)) != row['digest']:
            raise ProjectError('Required documentation changed after review: ' + path)
    inspected = pc.inspect_context(_root(root))
    deferred = docs.get('stale_source_conflicts', [])
    deferred_stale = (inspected['status'] == 'stale' and isinstance(deferred, list) and bool(deferred)
        and all(isinstance(c, str) and c.startswith('Guide source changed: ')
                and not any(c.startswith('Guide source changed: ' + p + ' <- ') for p in impact['paths']) for c in deferred)
        and sorted(deferred) == sorted(inspected['conflicts']))
    if (inspected['status'] != 'ready' and not deferred_stale) or docs.get('context_digest') != inspected.get('digest'):
        raise ProjectError('Published documentation context digest differs')


def _validate_receipt(root, task_id, run_id, receipt, *, live_publication=True):
    if not isinstance(receipt, dict):
        raise ProjectError('Completion requires a controller receipt object')
    if type(receipt.get('schema_version')) is not int or receipt['schema_version'] not in (1, 2):
        raise ProjectError('Completion receipt requires schema_version=1 or 2')
    with _db(root) as conn:
        starts = conn.execute("SELECT detail FROM events WHERE task_id=? AND run_id=? AND event='running'", (task_id, run_id)).fetchall()
    needs_plan = any((json.loads(row['detail']) or {}).get('requires_plan_review') is True for row in starts)
    if needs_plan and receipt['schema_version'] != 2:
        raise ProjectError('This run requires a schema-2 receipt with reviewed plan approval')
    if receipt['schema_version'] == 2:
        from .planning import PlanApproval, digest as plan_digest
        approval_file = _file_evidence(root, receipt.get('plan_approval_path'))
        approval = PlanApproval.model_validate(_read_json(_root(root) / approval_file))
        if approval.task_id != task_id or approval.run_id != run_id:
            raise ProjectError('Plan approval task/run binding differs')
        if receipt.get('plan_digest') != approval.plan_digest:
            raise ProjectError('Plan approval digest differs')
        directory = (_root(root) / approval_file).parent
        for name, expected in ((f'plan-{approval.version}.json', approval.plan_digest),
                               (f'review-{approval.version}.json', approval.review_digest)):
            evidence_file = _file_evidence(root, (directory / name).relative_to(_root(root)).as_posix())
            if plan_digest(_read_json(_root(root) / evidence_file)) != expected:
                raise ProjectError('Plan/review evidence differs from controller approval')
        approved_plan = _read_json(directory / f'plan-{approval.version}.json')
        _validate_documentation_receipt(root, receipt, approved_plan, live_publication=live_publication)
    if receipt.get('task_id') != task_id or receipt.get('run_id') != run_id:
        raise ProjectError('Completion receipt task_id and run_id must match the current task and controller run')
    for key in ('promoted', 'checks_passed', 'review_approved'):
        if receipt.get(key) is not True:
            raise ProjectError(f'Completion requires {key}=true')
    digest = receipt.get('source_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
        raise ProjectError('Completion requires a 64-character source_sha256')
    _text(receipt.get('summary'), 'Completion summary')
    changes = receipt.get('changed_files')
    if not isinstance(changes, list) or any(not isinstance(path, str) for path in changes):
        raise ProjectError('Completion requires changed_files as a list of paths')
    try:
        for path in changes:
            _relative_pattern(path)
            if '*' in path or '?' in path or path.endswith('/'):
                raise ProjectError('changed_files must contain concrete file paths')
    except ValueError as exc:
        raise ProjectError(str(exc)) from exc
    evidence = receipt.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise ProjectError('Completion requires a nonempty evidence list')
    for path in evidence:
        _file_evidence(root, path)
    path = receipt_path(root, task_id, run_id)
    try:
        _file_evidence(root, path.relative_to(_root(root)).as_posix())
        saved = _read_json(path)
    except (OSError, ScopeViolation, ValueError) as exc:
        raise ProjectError(f'Cannot validate saved controller receipt: {exc}') from exc
    if json.dumps(saved, sort_keys=True, allow_nan=False) != json.dumps(receipt, sort_keys=True, allow_nan=False):
        raise ProjectError('Saved controller receipt does not match completion receipt')
    # Reject NaN or non-JSON types even when a caller bypasses the CLI.
    json.dumps(receipt, ensure_ascii=False, allow_nan=False)
    return path.relative_to(_root(root)).as_posix()


def complete_task(root: Path, task_id: str, run_id: str, receipt: dict) -> dict:
    """Accept only a reviewed, checked, promoted controller result, never agent claims."""
    _id(task_id, 'Task ID')
    _id(run_id, 'Run ID')
    with _db(root, write=True) as conn:
        row = _row(conn, task_id)
        _owned(row, run_id)
        if row['status'] == 'completed':
            _validate_receipt(root, task_id, run_id, receipt, live_publication=False)
            if json.loads(row['receipt']) != receipt:
                raise ProjectError('Completed task receipt cannot be replaced')
            return _task(row)  # Exact completion replay is read-only and idempotent.
        receipt_file = _validate_receipt(root, task_id, run_id, receipt)
        if row['status'] != 'reviewing':
            raise ProjectError(f"Task must be reviewing before completion; current state: {row['status']}")
        evidence = list(dict.fromkeys([*receipt['evidence'], receipt_file]))
        changed = conn.execute("UPDATE tasks SET status='completed',summary=?,changed_files=?,evidence=?,receipt=?,"
            "blocker=NULL,updated_at=? WHERE id=? AND run_id=? AND status='reviewing'",
            (receipt['summary'], json.dumps(receipt['changed_files']), json.dumps(evidence),
             json.dumps(receipt, ensure_ascii=False), _now(), task_id, run_id)).rowcount
        if changed != 1:
            raise ProjectError('Task changed during completion')
        _event(conn, task_id, run_id, 'completed', receipt)
    return _refresh_after_commit(root, task_id)


def fail_task(root: Path, task_id: str, run_id: str, reason: str, *, attempt_summary=None) -> dict:
    _text(reason, 'Failure reason')
    with _db(root, write=True) as conn:
        row = _row(conn, task_id)
        _owned(row, run_id)
        if row['status'] == 'blocked' and row['blocker'] == reason:
            return _task(row)
        if row['status'] not in ACTIVE:
            raise ProjectError('Only an active task may be failed by its controller')
        conn.execute("UPDATE tasks SET status='blocked',blocker=?,updated_at=? WHERE id=? AND run_id=?",
                     (reason, _now(), task_id, run_id))
        _event(conn, task_id, run_id, 'blocked', reason)
        if attempt_summary is not None:
            _event(conn, task_id, run_id, 'attempt_summary', attempt_summary)
    return _refresh_after_commit(root, task_id)


def _manual_block(root, task_id, reason, recover):
    _text(reason, 'Block reason')
    from .session import acquire_repo_lock
    with acquire_repo_lock(_root(root)):
        with _db(root, write=True) as conn:
            row = _row(conn, task_id)
            if recover:
                if row['status'] not in ACTIVE:
                    raise ProjectError('Recovery requires an interrupted active task')
            elif row['status'] not in {'pending', 'blocked'}:
                raise ProjectError('Use recover for an interrupted task; completed tasks cannot be blocked')
            conn.execute("UPDATE tasks SET status='blocked',blocker=?,updated_at=? WHERE id=?",
                         (reason, _now(), task_id))
            _event(conn, task_id, row['run_id'], 'recovered' if recover else 'blocked', reason)
        return _refresh_after_commit(root, task_id)


def block_task(root: Path, task_id: str, reason: str) -> dict:
    return _manual_block(root, task_id, reason, False)


def recover_task(root: Path, task_id: str, reason: str) -> dict:
    """Mark interruption blocked; never infer successful acceptance from files."""
    return _manual_block(root, task_id, reason, True)


def _atomic_text(path, text):
    with _parent_fd(path, create=True) as (parent, name):
        _regular_at(parent, name, missing=True)
        temporary = f'.{name}.{secrets.token_hex(12)}.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _cell(text):
    return str(text).replace('|', '\\|').replace('\r', '').replace('\n', '<br>')


def _bullets(values):
    return '\n'.join('- ' + str(value).replace('\n', '\n  ') for value in values) if values else '- None.'


def _project_markdown(snapshot):
    project, totals = snapshot['project'], snapshot['totals']
    lines = [f"# {project['name']} — project status", '',
        'Generated from the controller-owned project ledger. Only reviewed, checked and promoted tasks count as completed.', '',
        f"**Accepted completion: {totals['completion_percent']}% (weighted).** "
        f"{totals['completed']}/{totals['tasks']} tasks completed; {totals['pending']} pending; "
        f"{totals['active']} active; {totals['blocked']} blocked.", '',
        '## Milestones', '', '| ID | Milestone | Status | Completed tasks | Weighted completion |',
        '| --- | --- | --- | --- | --- |']
    for milestone in snapshot['milestones']:
        lines.append(f"| {milestone['id']} | {_cell(milestone['title'])} | {milestone['status']} | "
                     f"{milestone['completed']}/{milestone['total']} | {milestone['completion_percent']}% |")
    lines += ['', '## Tasks', '', '| ID | Task | Milestone | Status | Run | Blocker |', '| --- | --- | --- | --- | --- | --- |']
    for task in snapshot['tasks']:
        lines.append(f"| [{task['id']}](task-reports/{task['id']}.md) | {_cell(task['title'])} | "
                     f"{task['milestone_id']} | {task['status']} | {task['run_id'] or '—'} | {_cell(task['blocker'] or '—')} |")
    lines += ['', '## Next ready tasks', '', _bullets(snapshot['next_tasks']), '',
              'Blocked tasks require the blocker to be resolved before explicitly starting a new run. '
              'Tasks with unfinished dependencies are never offered as ready.', '']
    observations = snapshot.get('methodology_metrics')
    if observations is not None:
        lines += ['', '## Methodology observations', '', 'Derived from controller attempts and receipts; unknown history stays unknown. Cost is informational.', '',
                  '```json', json.dumps(observations, ensure_ascii=False, indent=2), '```', '']
    return '\n'.join(lines)


def _task_markdown(task, history):
    receipt = task.get('receipt') or {}
    lines = [f"# {task['id']}: {task['title']}", '',
             f"Status: **{task['status']}** · Milestone: {task['milestone_id']} · Run: {task['run_id'] or 'not started'}", '',
             '## Original request', '', task['request'], '', '## Required deliverables', '', _bullets(task['deliverables']), '',
             '## Scope', '', 'Allowed paths:', '', _bullets(task['allowed_paths']), '', 'Constraints:', '',
             _bullets(task['constraints']), '', 'Non-goals:', '', _bullets(task['non_goals']), '',
             'Dependencies:', '', _bullets(task['dependencies']), '',
             'Additional required checks (added to workflow checks):', '', _bullets(task['required_checks']), '',
             '## Accepted result', '', task['summary'] if task['status'] == 'completed' else 'No accepted result yet.', '',
             'Changed files:', '', _bullets(task['changed_files']), '', '## Verification and review', '',
             f"Checks passed: {receipt.get('checks_passed', 'not accepted')}",
             f"Review approved: {receipt.get('review_approved', 'not accepted')}",
             f"Source promoted: {receipt.get('promoted', 'not accepted')}",
             f"Verified source SHA-256: {receipt.get('source_sha256', 'not accepted')}", '']
    context = receipt.get('project_context') or {}
    docs = receipt.get('documentation') or {}
    from urllib.parse import quote
    def link(label, path):
        if not isinstance(path, str) or not path:
            return label + ': not recorded'
        try:
            _relative_pattern(path)
        except ValueError:
            return label + ': recorded outside the project'
        return '[' + label + '](../../' + quote(path, safe='/') + ')'
    lines += ['## Lifecycle evidence', '',
              link('Project understanding', context.get('report_path')),
              link('Approved plan', receipt.get('plan_approval_path')),
              'Documentation: ' + str(docs.get('status', 'not recorded')),
              'Documentation decision: ' + str(docs.get('decision', 'not recorded')),
              str(docs.get('reason', '')), link('Documentation receipt', receipt.get('documentation_path')), '']
    if docs.get('paths'):
        lines += ['Documentation files:', '', *['- ' + link(path, path) for path in docs['paths']], '']
    if receipt.get('checks') is not None:
        lines += ['Check details:', '', '```json', json.dumps(receipt['checks'], ensure_ascii=False, indent=2), '```', '']
    if receipt.get('review') is not None:
        lines += ['Review details:', '', '```json', json.dumps(receipt['review'], ensure_ascii=False, indent=2), '```', '']
    flagged = (receipt.get('owner_review') or {}).get('flagged') or []
    floor = receipt.get('floor_guard') or []
    if flagged or floor:
        # Accepted without stopping; the independent reviewer ruled on each item.
        lines += ['## For the owner (after completion)', '',
                  'The task closed on its own. The reviewer ruled on each item below before approval; '
                  'read them when you review the work.', '']
        lines += [f"- Sensitive change — {_cell(f.get('category'))}: {_cell(f.get('why'))}" for f in flagged]
        lines += [f"- Quality bar — {_cell(f.get('rule'))} at {_cell(f.get('path'))}"
                  f"{':' + str(f['line']) if f.get('line') else ''} ({_cell(f.get('marker'))})" for f in floor]
        lines.append('')
    cost = receipt.get('cost')
    if isinstance(cost, dict):
        amount = f"${float(cost.get('usd') or 0):.2f}" + ('' if cost.get('complete') else ' (partial)')
        lines += ['## Cost', '', f"{amount}, {int(cost.get('tokens') or 0):,} tokens. {_cell(cost.get('note'))}. "
                  'Reported only; cost never stops a task.', '']
    handoffs = receipt.get('handoffs')
    if handoffs is None and isinstance(task.get('detail'), dict):
        handoffs = task['detail'].get('handoffs')
    if handoffs is not None:
        lines += ['## Agent handoffs', '', 'Controller-recorded agent reports; acceptance is determined by the task state above.', '',
                  '```json', json.dumps(handoffs, ensure_ascii=False, indent=2), '```', '']
    lines += ['## Evidence', '', _bullets(task['evidence']), '', '## Blocker', '', task['blocker'] or 'None.', '',
              '## Remaining work', '', 'Accepted and complete.' if task['status'] == 'completed' else
              'Complete and verify the required deliverables; obtain controller acceptance. ' +
              ('Resolve the blocker above first.' if task['blocker'] else 'Respect the dependencies and declared scope.'), '',
              '## Controller history', '', '| Time (UTC) | Event | Run | Detail |', '| --- | --- | --- | --- |']
    for event in history:
        lines.append(f"| {event['created_at']} | {event['event']} | {event['run_id'] or '—'} | "
                     f"{_cell(json.loads(event['detail']))} |")
    return '\n'.join(lines) + '\n'


def refresh_reports(root: Path) -> dict:
    """Rebuild all reports from committed state; serialize projections with writers.

    A failed report write does not roll back a previously committed task result.
    Re-run this function or the CLI report command to repair any stale projection.
    """
    root = _root(root)
    with _db(root, write=True) as conn:
        snapshot = _snapshot(conn)
        for task in snapshot['tasks']:
            history = conn.execute('SELECT * FROM events WHERE task_id=? ORDER BY sequence', (task['id'],)).fetchall()
            _atomic_text(root / '.kh/task-reports' / (task['id'] + '.md'), _task_markdown(task, history))
        _atomic_text(root / '.kh/PROJECT_STATUS.md', _project_markdown(snapshot))
        _atomic_json(root / '.kh/project-status.json', snapshot)
    return snapshot
