#!/usr/bin/env python3
"""Read offline ledger/journal evidence and print a stop baseline and blocker report.

This distribution-side inspector never imports the runtime or dispatches work.
SQLite is opened on a private copy, so even sidecar creation cannot touch inputs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from urllib.parse import quote


ACTIONS = {
    'owner_decision': 'Review the evidence and use the existing intent/disposition acceptance path. '
                      'This report records no approval.',
    'permissions': 'Inspect the recorded scope or permission rejection. Do not expand permissions '
                   'or scope to make it pass.',
    'quality': 'Correct the recorded delivery, check or review failure through the existing guarded '
               'cycle, with its required checks and independent review.',
    'environment': 'Resolve the recorded environment/provider prerequisite within existing '
                   'authorization before explicitly continuing work.',
    'interruption': 'Inspect interrupted work and confirm process cleanup before the existing '
                    'recovery path. No task is accepted or restarted by this report.',
    'limits': 'Inspect the exhausted deadline or attempt budget. This report cannot reset limits '
              'or authorize another attempt.',
    'unknown': 'Inspect the linked original record. The cause and required owner decision are '
               'not established; do not infer approval or an automatic retry.',
}


def classify(reason, event):
    """Recognize controller markers only; free-form model discussion stays unknown."""
    if event == 'recovered':
        return 'interruption'
    for category, prefixes in (
        ('owner_decision', ('OWNER_DECISION_REQUIRED:', 'OWNER_REVIEW_REQUIRED:')),
        ('environment', ('INFRA_OR_PROVIDER_BLOCKER:', 'INFRA_BOUNDARY:')),
        ('limits', ('BUDGET_EXHAUSTED:', 'EXECUTION_DEADLINE:', 'DEADLINE_INSUFFICIENT:',
                    'CANDIDATE_CHECKED_UNREVIEWED:', 'ROLE TIMEOUT:', 'RECOVERY_NO_PROGRESS:',
                    'Task was not accepted within the configured repair budget')),
        ('quality', ('REVIEW_EXISTING_REJECTED:', 'REVIEW_EXISTING_CHECKS_FAILED:',
                     'PLAN_REVISION_REQUIRED:', 'VERIFICATION_SOURCE_CHANGED:')),
    ):
        if reason.startswith(prefixes):
            return category
    if re.match(r'^(builder|reviewer|documenter|planner|plan_reviewer|project_architect) '
                r'modified prohibited paths\b', reason):
        return 'permissions'
    if re.match(r'^\w+ failed gates after \d+ attempt', reason):
        return 'quality'
    return 'unknown'


def read_input(root, path):
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('Refusing symlinked input: ' + str(path.relative_to(root)))
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('Input must be a regular, unlinked file')
    with path.open('rb') as stream:
        data = stream.read()
        after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError('Input changed during the offline inspection')
    return data


def source(root, path, data):
    return {'path': path.relative_to(root).as_posix(),
            'sha256': hashlib.sha256(data).hexdigest()}


def read_ledger(root, path):
    def stable():
        for suffix in ('-wal', '-journal'):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                if read_input(root, sidecar):
                    raise ValueError('Use a quiescent offline ledger; a nonempty SQLite sidecar exists')
    stable()
    data = read_input(root, path)
    with tempfile.TemporaryDirectory(prefix='kh-stop-inspect-') as temporary:
        copy = Path(temporary) / 'project.db'
        copy.write_bytes(data)
        connection = sqlite3.connect(copy.as_uri() + '?mode=ro&immutable=1', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported project ledger schema')
            tasks = [dict(row) for row in connection.execute(
                'SELECT id,status,run_id,blocker,updated_at FROM tasks ORDER BY position')]
            events = [dict(row) for row in connection.execute(
                'SELECT sequence,task_id,run_id,event,detail,created_at FROM events ORDER BY sequence')]
        except sqlite3.Error as error:
            raise ValueError('Cannot read project ledger: ' + str(error)) from error
        finally:
            connection.close()
    stable()
    if read_input(root, path) != data:
        raise ValueError('Project ledger changed during the offline inspection')
    return tasks, events, source(root, path, data)


def read_journals(root):
    journals, sources = {}, []
    directory = root / 'adws/adw_data/sessions'
    # Refuse a symlink at any existing parent, including an empty sessions folder.
    for part in (directory, *directory.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('Refusing symlinked sessions directory')
    for path in sorted(directory.glob('*/events.jsonl')):
        data = read_input(root, path)
        rows = []
        for number, line in enumerate(data.decode('utf-8').splitlines(), 1):
            try:
                row = json.loads(line)
                if (not isinstance(row, dict) or row.get('adw_id') != path.parent.name
                        or not isinstance(row.get('type'), str) or not row.get('type')
                        or not isinstance(row.get('name'), str)
                        or not isinstance(row.get('payload'), dict)):
                    raise ValueError('Invalid journal event or run identity')
            except ValueError as error:
                raise ValueError(f'Invalid journal {path.relative_to(root)}:{number}') from error
            rows.append({**row, 'line': number})
        ref = source(root, path, data)
        sources.append(ref)
        journals[path.parent.name] = (rows, ref)
    return journals, sources


def stop_record(task, run, reason, event, at, evidence):
    text = reason if isinstance(reason, str) and reason.strip() else ''
    category = classify(text, event)
    return {'task_id': task, 'run_id': run, 'at': at, 'category': category,
            'reason': text or 'Unknown', 'required_action': ACTIONS[category],
            'phase': None, 'gate': None, 'violations': [], 'evidence': [evidence],
            'currently_blocked': None}


def analyze(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Project root must be a directory')
    path = root / '.kh/project.db'
    ledger = path.exists() or path.is_symlink()
    tasks, events, stops, sources, notes = [], [], [], [], []
    if ledger:
        tasks, events, ref = read_ledger(root, path)
        sources.append(ref)
        for event in events:
            if event['event'] in ('blocked', 'recovered'):
                reason = json.loads(event['detail'])
                stops.append(stop_record(event['task_id'], event['run_id'], reason, event['event'],
                    event['created_at'], {**ref, 'record': f"events/{event['sequence']}"}))
    known_runs = {event['run_id']: event['task_id'] for event in events if event['run_id']}
    known_runs.update({t['run_id']: t['id'] for t in tasks if t['run_id']})
    journals, journal_sources = read_journals(root)
    sources.extend(journal_sources)
    for run, (rows, ref) in journals.items():
        terminal = [r for r in rows if r.get('type') == 'error'
                    and r.get('name') in ('guarded_task', 'not_accepted')]
        if not terminal:
            continue  # A repaired role error or gate rejection is not a terminal stop.
        end = terminal[-1]
        reason = end['payload'].get('error') or end['payload'].get('reason')
        existing = [s for s in stops if s['run_id'] == run]
        if not existing:
            if ledger and run in known_runs:
                notes.append('Missing ledger stop event for run ' + run)
            stops.append(stop_record(known_runs.get(run), run, reason, 'journal_stop', end.get('ts'),
                                     {**ref, 'line': end['line']}))
            existing = [stops[-1]]
        for stop in existing:
            if not isinstance(reason, str) or not reason or not stop['reason'].startswith(reason):
                notes.append('Journal terminal reason does not match the ledger stop for run ' + run)
                continue
            matching = [r for r in rows if r.get('type') == 'error'
                        and r.get('name') not in ('guarded_task', 'not_accepted')
                        and isinstance(r['payload'].get('error'), str)
                        and r['payload']['error'] and stop['reason'].startswith(r['payload']['error'])]
            detail = matching[-1] if matching else end
            stop['phase'] = detail.get('name')
            failed = [r for r in rows if r.get('type') == 'gate_fail'
                      and r.get('phase_id') and r.get('phase_id') == detail.get('phase_id')
                      and r['line'] < detail['line']]
            if failed:
                gate = failed[-1]
                stop['gate'] = gate.get('name')
                stop['violations'] = gate['payload'].get('violations', [])
            if not any(e.get('path') == ref['path'] and e.get('line') == detail['line']
                       for e in stop['evidence']):
                stop['evidence'].append({**ref, 'line': detail['line']})
    for task in tasks:
        history = [s for s in stops if s['task_id'] == task['id'] and s['run_id'] == task['run_id']]
        if task['status'] == 'blocked' and not history:
            notes.append('Missing stop history for blocked task ' + task['id'])
            stops.append(stop_record(task['id'], task['run_id'], task['blocker'], 'snapshot',
                task['updated_at'], {**sources[0], 'record': 'tasks/' + task['id']}))
        history = [s for s in stops if s['task_id'] == task['id']]
        current = [s for s in history if s['run_id'] == task['run_id']]
        for stop in history:
            stop['currently_blocked'] = bool(current and stop is current[-1]
                                             and task['status'] == 'blocked')
    counts = Counter(s['category'] for s in stops)
    known = bool(sources)
    return {'schema_version': 1, 'root': root.as_posix(), 'observational_only': True, 'provider_calls': 0,
        'coverage': 'partial' if notes else 'recorded_only' if known else 'unknown',
        'recorded_stops': len(stops) if known else None, 'human_interventions': None,
        'counts': {category: counts[category] if known else None for category in ACTIONS},
        'last_stop_at': max((s['at'] for s in stops if s['at']), default=None),
        'open_blockers': [s for s in stops if s['currently_blocked']] if ledger else None,
        'stops': stops, 'sources': sources, 'notes': notes + [
            'Recorded stops are not measured human interventions; unknown history is not reconstructed.',
            'Repaired role errors and gate failures are not counted as terminal stops.',
            'Only .kh/project.db and default adws/adw_data/sessions/*/events.jsonl are inspected.',
            'No ledger means current task state is unknown. Missing phase/gate evidence stays unknown.']}


def markdown(report):
    def cell(value):
        return html.escape(str(value if value is not None else 'Unknown')).replace('|', '\\|').replace('\n', '<br>')
    lines = ['# Intervention baseline and blocker report', '',
             'Observational only. This report cannot approve, retry or change a task.', '',
             f"Coverage: {report['coverage']}. Recorded stops: {cell(report['recorded_stops'])}. "
             'Human interventions: Unknown.', '', '| Cause | Recorded stops |', '| --- | --- |']
    lines += [f'| {name} | {cell(count)} |' for name, count in report['counts'].items()]
    lines += ['', '## Recorded stops', '']
    for stop in report['stops']:
        lines += [f"### {cell(stop['task_id'])} / {cell(stop['run_id'])}", '',
            f"Currently blocked: {cell(stop['currently_blocked'])}. Phase: {cell(stop['phase'])}. "
            f"Gate: {cell(stop['gate'])}.", '', 'Reason: ' + cell(stop['reason']), '',
            'Required action: ' + stop['required_action'], '', 'Evidence:', '']
        lines += [f"- [{cell(e['path'])}](<{quote(str(Path(report['root']) / e['path']), safe='/')}>), "
                  f"{cell(e.get('record') or e.get('line'))}; SHA-256 {e['sha256']}"
                  for e in stop['evidence']]
        lines += ['- ' + cell(v) for v in stop['violations']]
        lines.append('')
    lines += ['## Coverage limits', '', *['- ' + note for note in report['notes']], '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--format', choices=('json', 'markdown'), default='markdown')
    args = parser.parse_args()
    try:
        report = analyze(args.root)
    except (OSError, ValueError, sqlite3.Error) as error:
        print('Offline intervention report failed: ' + str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.format == 'json' else markdown(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
