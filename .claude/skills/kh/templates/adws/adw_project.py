#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Initialize and inspect the controller-owned project plan and progress ledger."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import sys

from adw_modules import git_helper, project, session
from adw_modules.scope import _parent_fd, _read_json


CONTROL_SCHEMA_VERSION = 1
CONTROL_STATES = frozenset({'running', 'paused', 'complete', 'stopped', 'interrupted'})


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _control_path(root, name):
    return Path(root).resolve() / '.kh' / name


def _write_control(root, name, value):
    from adw_modules.scope import _atomic_json
    _atomic_json(_control_path(root, name), value)


@contextmanager
def _control_guard(root):
    """Serialize pause requests with the decision to dispatch the next task."""
    with _parent_fd(_control_path(root, 'controller-control.lock'), create=True) as (parent, name):
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'a+') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError('Refusing non-regular controller control lock')
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield


def _validate_controller_state(value):
    if not isinstance(value, dict) or value.get('schema_version') != CONTROL_SCHEMA_VERSION:
        raise ValueError('Invalid controller state schema')
    if value.get('state') not in CONTROL_STATES:
        raise ValueError('Invalid controller state')
    controller_id = value.get('controller_id')
    if not isinstance(controller_id, str) or not controller_id:
        raise ValueError('Invalid controller identity')
    return value


def controller_status(root):
    """Read cooperative controller state without changing the project ledger."""
    root = Path(root).resolve()
    project.status(root)  # Refuse missing/corrupt project state before reading control files.
    try:
        return _validate_controller_state(_read_json(_control_path(root, 'controller-state.json')))
    except FileNotFoundError:
        return {'schema_version': CONTROL_SCHEMA_VERSION, 'state': 'idle', 'controller_id': None}


def request_pause_after_current(root):
    """Ask the active continuous controller to stop before dispatching another task."""
    root = Path(root).resolve()
    with _control_guard(root):
        state = controller_status(root)
        if state['state'] != 'running':
            raise RuntimeError('No running continuous controller can be paused')
        try:
            probe = session.acquire_repo_lock(root)
        except RuntimeError as error:
            if not str(error).startswith('BUSY:'):
                raise
        else:
            probe.close()
            raise RuntimeError('Controller state is stale; no controller process holds the repository lock')
        request = {'schema_version': CONTROL_SCHEMA_VERSION,
                   'controller_id': state['controller_id'],
                   'action': 'pause_after_current', 'requested_at': _now()}
        _write_control(root, 'controller-request.json', request)
        return request


def _pause_requested(root, controller_id):
    try:
        request = _read_json(_control_path(root, 'controller-request.json'))
    except FileNotFoundError:
        return False
    if not isinstance(request, dict) or request.get('schema_version') != CONTROL_SCHEMA_VERSION:
        raise ValueError('Invalid controller request schema')
    if request.get('controller_id') != controller_id:
        return False  # A stale request must never pause a later controller run.
    action = request.get('action')
    if action not in ('run', 'pause_after_current'):
        raise ValueError('Invalid controller request action')
    return action == 'pause_after_current'


def _controller_state(controller_id, state, **detail):
    return {'schema_version': CONTROL_SCHEMA_VERSION, 'controller_id': controller_id,
            'state': state, 'updated_at': _now(), **detail}


def _stdout_notification(event):
    print('KH_TASK_COMPLETED ' + json.dumps(event, ensure_ascii=False), flush=True)


def _check_run_choices(root, config, use):
    """Refuse a malformed per-run model choice before the batch takes the lock."""
    if not use:
        return
    from adw_modules import agents, routing
    path = Path(config) if Path(config).is_absolute() else Path(root) / config
    routing.run_choices(agents.load_config(str(path)), use)


def run_continuous(root, *, wait_seconds=0, mode='full',
                   config='adws/adw_kh_config/kh.config.yaml', profile=None,
                   notify=None, use=None):
    """Run ready tasks one-by-one until verified completion, pause, or failure.

    There is no arbitrary task-count ceiling. The
    recorded plan is finite and held unchanged during execution, so completion still has a bounded
    definition. Acceptance remains owned by ``adw_start`` and the project
    ledger; a worker claim alone cannot advance the loop.
    """
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
        raise ValueError('wait_seconds must be an integer in 0..60')
    if mode not in ('full', 'light'):
        raise ValueError('mode must be full or light')
    if notify is not None and not callable(notify):
        raise ValueError('notify must be callable')
    notify = notify or _stdout_notification
    root = Path(root).resolve()
    project.require_sequential(root)
    _check_run_choices(root, config, use)
    import adw_start
    controller_id = secrets.token_hex(12)
    result = {'mode': mode, 'controller_id': controller_id, 'attempted': [],
              'status': 'complete', 'exit_code': 0}
    previous_cwd = Path.cwd()
    state_detail = {'mode': mode, 'pid': os.getpid(), 'current_task': None,
                    'completed_this_run': 0}
    try:
        with session.acquire_repo_lock(root, wait_seconds=wait_seconds) as lease:
            snapshot = project.status(root)
            with _control_guard(root):
                _write_control(root, 'controller-state.json',
                               _controller_state(controller_id, 'running', **state_detail))
                _write_control(root, 'controller-request.json', {
                    'schema_version': CONTROL_SCHEMA_VERSION, 'controller_id': controller_id,
                    'action': 'run', 'requested_at': _now()})
            try:
                os.chdir(root)
                while True:
                    snapshot = project.status(root)
                    totals = snapshot['totals']
                    if totals['completed'] == totals['tasks']:
                        result['status'] = 'complete'
                        _write_control(root, 'controller-state.json', _controller_state(
                            controller_id, 'complete', **state_detail,
                            completion_percent=totals['completion_percent']))
                        break
                    states = {task['id']: task['status'] for task in snapshot['tasks']}
                    selected = next((task_id for task_id in snapshot['next_tasks']
                                     if states.get(task_id) == 'pending'), None)
                    if selected is None:
                        result.update(status='stopped', exit_code=1,
                                      reason='No ready pending task; resolve blocked or interrupted work')
                        _write_control(root, 'controller-state.json', _controller_state(
                            controller_id, 'stopped', **state_detail, reason=result['reason']))
                        break
                    with _control_guard(root):
                        if _pause_requested(root, controller_id):
                            result['status'] = 'paused'
                            _write_control(root, 'controller-state.json', _controller_state(
                                controller_id, 'paused', **state_detail,
                                reason='Owner requested pause before the next task'))
                            break
                        state_detail['current_task'] = selected
                        _write_control(root, 'controller-state.json',
                                       _controller_state(controller_id, 'running', **state_detail))
                    entry = {'task_id': selected}
                    result['attempted'].append(entry)
                    try:
                        code = adw_start.main(config=config, profile=profile, task_id=selected, use=use,
                                              repo_lock=lease, mode=mode, refresh_context=True)
                        actual = project.load_task(root, selected)
                        entry.update(exit_code=code, status=actual['status'], run_id=actual.get('run_id'))
                        if code != 0 or actual['status'] != 'completed':
                            entry['reason'] = actual.get('blocker') or 'Task did not reach controller-verified completion'
                            result.update(status='stopped', exit_code=1)
                            state_detail['current_task'] = None
                            _write_control(root, 'controller-state.json', _controller_state(
                                controller_id, 'stopped', **state_detail, reason=entry['reason']))
                            break
                    except (ValueError, RuntimeError, OSError) as error:
                        entry.update(status='stopped', reason=str(error), exit_code=1)
                        result.update(status='stopped', exit_code=1)
                        state_detail['current_task'] = None
                        _write_control(root, 'controller-state.json', _controller_state(
                            controller_id, 'stopped', **state_detail, reason=str(error)))
                        break

                    snapshot = project.status(root)
                    totals = snapshot['totals']
                    state_detail['current_task'] = None
                    state_detail['completed_this_run'] += 1
                    next_tasks = list(snapshot['next_tasks'])
                    event = {'event': 'task_completed', 'task_id': selected,
                             'title': actual['title'], 'summary': actual.get('summary') or '',
                             'run_id': actual.get('run_id'),
                             'completed_tasks': totals['completed'],
                             'total_tasks': totals['tasks'],
                             'completion_percent': totals['completion_percent'],
                             'next_task': next_tasks[0] if next_tasks else None}
                    _write_control(root, 'controller-state.json', _controller_state(
                        controller_id, 'running', **state_detail, last_completed=event))
                    try:
                        notify(event)
                    except Exception as error:
                        entry['notification_error'] = str(error)
                        result.update(status='stopped', exit_code=1,
                                      reason='Owner notification failed; no later task was dispatched')
                        _write_control(root, 'controller-state.json', _controller_state(
                            controller_id, 'stopped', **state_detail, reason=result['reason']))
                        break
                    with _control_guard(root):
                        if _pause_requested(root, controller_id):
                            result['status'] = 'paused'
                            _write_control(root, 'controller-state.json', _controller_state(
                                controller_id, 'paused', **state_detail,
                                reason='Owner requested pause after the current task'))
                            break
            finally:
                os.chdir(previous_cwd)
    except BaseException:
        try:
            _write_control(root, 'controller-state.json', _controller_state(
                controller_id, 'interrupted', **state_detail,
                reason='Continuous controller was interrupted'))
        except Exception:
            pass
        raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='Project repository root (default: current repository)')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Import the complete approved project plan once')
    init.add_argument('--plan', required=True, type=Path)
    init.add_argument('--dry-run', action='store_true', help='Validate the plan without creating files')
    amend = commands.add_parser('amend', help='Revise one pending or blocked task using an accepted owner intent')
    amend.add_argument('--task', required=True)
    amend.add_argument('--definition', required=True, type=Path, help='JSON task definition retaining the task ID')
    amend.add_argument('--reason', required=True)
    amend.add_argument('--dry-run', action='store_true', help='Validate and preview the revision without writes')
    split = commands.add_parser('split', help='Split a pending or blocked task into accepted smaller units')
    split.add_argument('--task', required=True)
    split.add_argument('--plan', required=True, type=Path, help='JSON list of replacement task definitions')
    split.add_argument('--reason', required=True)
    split.add_argument('--dry-run', action='store_true', help='Validate and preview the split without writes')
    commands.add_parser('status', help='Read current project status without modifying files')
    commands.add_parser('next', help='Read pending tasks whose dependencies are completed')
    continuous = commands.add_parser('run-continuous', help='Execute the recorded plan sequentially until completion, pause, or failure')
    continuous.add_argument('--wait-seconds', type=int, choices=range(0, 61), default=0)
    continuous.add_argument('--mode', choices=('full', 'light'), default='full')
    continuous.add_argument('--config', default='adws/adw_kh_config/kh.config.yaml')
    continuous.add_argument('--profile')
    continuous.add_argument('--use', action='append', metavar='ROLE=RUNTIME:MODEL:EFFORT',
                            help='Model and effort for one role in every task of this run (repeatable)')
    commands.add_parser('pause-after-current', help='Pause an active continuous run after its current task')
    commands.add_parser('controller-status', help='Read continuous-controller status without starting work')
    commands.add_parser('report', help='Rebuild Markdown and JSON reports from the durable ledger')
    for name in ('block', 'recover'):
        command = commands.add_parser(name, help='Record a blocker' if name == 'block' else 'Recover an interrupted run as blocked')
        command.add_argument('--task', required=True)
        command.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        root = args.root or git_helper.repo_root()
        if args.command == 'init':
            result = project.init_project(root, _read_json(args.plan), dry_run=args.dry_run)
        elif args.command == 'amend':
            result = project.amend_task(root, args.task, _read_json(args.definition),
                                        reason=args.reason, dry_run=args.dry_run)
        elif args.command == 'split':
            result = project.split_task(root, args.task, _read_json(args.plan),
                                        reason=args.reason, dry_run=args.dry_run)
        elif args.command == 'status':
            result = project.status(root)
        elif args.command == 'next':
            snapshot = project.status(root)
            ready = set(snapshot['next_tasks'])
            result = {'project': snapshot['project'], 'next_tasks': [task for task in snapshot['tasks'] if task['id'] in ready]}
        elif args.command == 'run-continuous':
            result = run_continuous(root, wait_seconds=args.wait_seconds, mode=args.mode,
                                    config=args.config, profile=args.profile, use=args.use)
        elif args.command == 'pause-after-current':
            result = request_pause_after_current(root)
        elif args.command == 'controller-status':
            result = controller_status(root)
        elif args.command == 'report':
            result = project.refresh_reports(root)
        elif args.command == 'block':
            result = project.block_task(root, args.task, args.reason)
        else:
            result = project.recover_task(root, args.task, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get('exit_code', 0) if args.command == 'run-continuous' else 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'Project command failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
