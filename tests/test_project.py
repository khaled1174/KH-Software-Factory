"""Real SQLite project tracking, recovery, receipt and report regressions."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADWS = ROOT / '.claude/skills/kh/templates/adws'
sys.path.insert(0, str(ADWS))
from adw_modules import project, session
from adw_modules.scope import _atomic_json


def plan():
    task = {'id': 'T1', 'milestone_id': 'M1', 'title': 'Foundation', 'request': 'Build the foundation',
            'deliverables': ['The foundation works'], 'allowed_paths': ['src/'], 'required_checks': ['test'], 'weight': 1}
    return {'schema_version': 1, 'project': {'id': 'app', 'name': 'Example | application'},
            'milestones': [{'id': 'M1', 'title': 'Foundation'}, {'id': 'M2', 'title': 'Integration'}],
            'tasks': [task, {**task, 'id': 'T2', 'milestone_id': 'M2', 'title': 'Integration',
                            'dependencies': ['T1'], 'weight': 3}]}


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo with spaces'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def init(self):
        return project.init_project(self.root, plan())

    def files(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob('*') if path.is_file()}

    def receipt(self, task='T1', run='run1', **updates):
        evidence = self.root / 'adws/adw_data/sessions' / run / 'checks.json'
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text('{"test": "passed"}\n')
        receipt = {'schema_version': 1, 'task_id': task, 'run_id': run,
            'promoted': True, 'checks_passed': True, 'review_approved': True,
            'source_sha256': 'a' * 64, 'summary': 'Implemented the accepted foundation',
            'changed_files': ['src/main.py'], 'evidence': [evidence.relative_to(self.root).as_posix()],
            'checks': [{'name': 'test', 'passed': True}], 'review': {'approved': True}, **updates}
        _atomic_json(project.receipt_path(self.root, task, run), receipt)
        return receipt

    def reviewing(self, task='T1', run='run1'):
        project.begin_task(self.root, task, run)
        project.transition(self.root, task, run, 'verifying')
        project.transition(self.root, task, run, 'reviewing')

    def complete(self, task='T1', run='run1'):
        self.reviewing(task, run)
        return project.complete_task(self.root, task, run, self.receipt(task, run))

    def test_dry_run_validates_without_any_files(self):
        before = self.files()
        result = project.init_project(self.root, plan(), dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['next_tasks'], ['T1'])
        self.assertEqual(self.files(), before)
        self.assertFalse((self.root / '.kh').exists())

    def test_invalid_plans_have_no_side_effects(self):
        cases = []
        bad = plan(); bad['tasks'][1]['dependencies'] = ['missing']; cases.append(bad)
        bad = plan(); bad['tasks'][0]['dependencies'] = ['T2']; cases.append(bad)
        bad = plan(); bad['tasks'][0]['dependencies'] = ['T1']; cases.append(bad)
        bad = plan(); bad['tasks'][1]['id'] = 'T1'; cases.append(bad)
        bad = plan(); bad['milestones'][1]['id'] = 'M1'; cases.append(bad)
        bad = plan(); bad['tasks'][0]['milestone_id'] = 'missing'; cases.append(bad)
        bad = plan(); bad['unknown_option'] = True; cases.append(bad)
        bad = plan(); bad['tasks'][0]['allowed_path'] = ['src/']; cases.append(bad)
        for field, value in [('allowed_paths', []), ('allowed_paths', ['../outside']), ('request', ' '),
                             ('deliverables', []), ('required_checks', ['typo']), ('weight', 0),
                             ('weight', -1), ('weight', True), ('weight', float('nan')), ('weight', float('inf')),
                             ('id', '../escape'), ('dependencies', ['T2', 'T2'])]:
            bad = plan(); bad['tasks'][0][field] = value; cases.append(bad)
        for bad in cases:
            with self.subTest(plan=bad), self.assertRaises(project.ProjectError):
                project.init_project(self.root, bad)
            self.assertFalse((self.root / '.kh').exists())

    def test_initialization_and_readonly_status(self):
        snapshot = self.init()
        self.assertEqual(snapshot['totals'], {'tasks': 2, 'completed': 0, 'pending': 2, 'active': 0,
                                             'blocked': 0, 'completion_percent': 0.0})
        before = self.files()
        for _ in range(2):
            self.assertEqual(project.status(self.root), snapshot)
            self.assertEqual(project.load_task(self.root, 'T1')['request'], 'Build the foundation')
        self.assertEqual(self.files(), before)
        with self.assertRaises(project.ProjectError):
            self.init()
        self.assertEqual(self.files(), before)

    def test_missing_status_does_not_create_database(self):
        with self.assertRaises(project.ProjectError):
            project.status(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_begin_checks_dependencies_and_single_active_task(self):
        self.init()
        with self.assertRaisesRegex(project.ProjectError, 'incomplete dependencies'):
            project.begin_task(self.root, 'T2', 'run2')
        result = project.begin_task(self.root, 'T1', 'run1')
        self.assertEqual(result['status'], 'running')
        with self.assertRaises(project.ProjectError):
            project.begin_task(self.root, 'T1', 'run1')
        self.assertEqual(project.status(self.root)['totals']['active'], 1)
        with self.assertRaises(project.ProjectError):
            project.transition(self.root, 'T1', 'run1', 'completed')

    def test_racing_starts_have_one_transaction_winner(self):
        self.init()
        def attempt(run):
            try:
                project.begin_task(self.root, 'T1', run)
                return 'started'
            except project.ProjectError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ['run-a', 'run-b']))
        self.assertEqual(sorted(results), ['rejected', 'started'])
        with sqlite3.connect(self.root / '.kh/project.db') as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    def test_independent_task_waits_for_active_controller_task(self):
        definition = plan(); definition['tasks'][1]['dependencies'] = []
        project.init_project(self.root, definition)
        project.begin_task(self.root, 'T1', 'run1')
        with self.assertRaisesRegex(project.ProjectError, 'Another project task is active'):
            project.begin_task(self.root, 'T2', 'run2')
        project.fail_task(self.root, 'T1', 'run1', 'Blocked foundation')
        self.assertEqual(project.begin_task(self.root, 'T2', 'run2')['status'], 'running')

    def test_mid_transaction_failure_rolls_back_task_and_attempt(self):
        self.init()
        before = project.status(self.root)
        with patch.object(project, '_event', side_effect=sqlite3.OperationalError('injected failure')):
            with self.assertRaises(project.ProjectError):
                project.begin_task(self.root, 'T1', 'run1')
        self.assertEqual(project.status(self.root), before)
        # No orphaned run ownership survived the failed transaction.
        self.assertEqual(project.begin_task(self.root, 'T1', 'run1')['status'], 'running')

    def test_completion_validates_all_gates_and_saved_receipt(self):
        self.init(); self.reviewing()
        valid = self.receipt()
        for key, bad_value in [('promoted', False), ('checks_passed', False), ('review_approved', False),
                               ('checks_passed', 1), ('source_sha256', 'wrong'), ('evidence', []),
                               ('evidence', ['missing.txt']), ('summary', ''), ('changed_files', ['../outside'])]:
            with self.subTest(key=key), self.assertRaises(project.ProjectError):
                project.complete_task(self.root, 'T1', 'run1', {**valid, key: bad_value})
        with self.assertRaisesRegex(project.ProjectError, 'does not match'):
            project.complete_task(self.root, 'T1', 'run1', {**valid, 'summary': 'a different claim'})
        project.receipt_path(self.root, 'T1', 'run1').unlink()
        with self.assertRaises(project.ProjectError):
            project.complete_task(self.root, 'T1', 'run1', valid)
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'reviewing')

    def test_weighted_progress_reports_and_completion_replay(self):
        self.init()
        task = self.complete()
        snapshot = project.status(self.root)
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(snapshot['totals']['completion_percent'], 25.0)
        self.assertEqual(snapshot['totals']['completed'], 1)
        self.assertEqual(snapshot['next_tasks'], ['T2'])
        self.assertEqual(snapshot['milestones'][0]['status'], 'completed')
        self.assertEqual(snapshot['milestones'][0]['completion_percent'], 100.0)
        self.assertEqual(json.loads((self.root / '.kh/project-status.json').read_text()), snapshot)
        report = (self.root / '.kh/task-reports/T1.md').read_text()
        for expected in ['Build the foundation', 'Implemented the accepted foundation', 'src/main.py',
                         'Checks passed: True', 'Review approved: True', 'completed', 'task-receipts/T1/run1.json']:
            self.assertIn(expected, report)
        before = self.files()
        replay = project.complete_task(self.root, 'T1', 'run1', task['receipt'])
        self.assertEqual(replay['status'], 'completed')
        self.assertEqual(self.files(), before)
        self.complete('T2', 'run2')
        self.assertEqual(project.status(self.root)['totals']['completion_percent'], 100.0)

    def test_saved_receipt_identity_cannot_accept_another_task_or_run(self):
        self.init(); self.reviewing()
        valid = self.receipt()
        cases = [{'schema_version': 999}, {'schema_version': True}, {'schema_version': 1.0},
                 {'task_id': 'OTHER'}, {'run_id': 'OTHER'},
                 {'schema_version': 999, 'task_id': 'OTHER', 'run_id': 'OTHER'}]
        for change in cases:
            malicious = {**valid, **change}
            # The saved receipt agrees with the argument: identity must still be rejected.
            _atomic_json(project.receipt_path(self.root, 'T1', 'run1'), malicious)
            with self.subTest(change=change), self.assertRaises(project.ProjectError):
                project.complete_task(self.root, 'T1', 'run1', malicious)
            self.assertEqual(project.load_task(self.root, 'T1')['status'], 'reviewing')
        for field in ('schema_version', 'task_id', 'run_id'):
            missing = {key: value for key, value in valid.items() if key != field}
            _atomic_json(project.receipt_path(self.root, 'T1', 'run1'), missing)
            with self.subTest(missing=field), self.assertRaises(project.ProjectError):
                project.complete_task(self.root, 'T1', 'run1', missing)
        _atomic_json(project.receipt_path(self.root, 'T1', 'run1'), valid)
        self.assertEqual(project.complete_task(self.root, 'T1', 'run1', valid)['status'], 'completed')

    def test_stale_run_cannot_complete_after_recovery(self):
        self.init(); self.reviewing()
        old_receipt = self.receipt()
        recovered = project.recover_task(self.root, 'T1', 'Controller was interrupted')
        self.assertEqual(recovered['status'], 'blocked')
        self.assertEqual(project.status(self.root)['totals']['completion_percent'], 0.0)
        with self.assertRaises(project.ProjectError):
            project.complete_task(self.root, 'T1', 'run1', old_receipt)
        with self.assertRaisesRegex(project.ProjectError, 'already been used'):
            project.begin_task(self.root, 'T1', 'run1')
        self.reviewing(run='run2')
        with self.assertRaisesRegex(project.ProjectError, 'Stale'):
            project.complete_task(self.root, 'T1', 'run1', old_receipt)
        with self.assertRaisesRegex(project.ProjectError, 'Stale'):
            project.fail_task(self.root, 'T1', 'run1', 'late failure')
        self.assertEqual(project.load_task(self.root, 'T1')['run_id'], 'run2')

    def test_failure_and_repair_never_count_as_completion(self):
        self.init(); project.begin_task(self.root, 'T1', 'run1')
        project.transition(self.root, 'T1', 'run1', 'needs_repair', {'summary': 'Tests failed'})
        self.assertEqual(project.status(self.root)['totals']['completion_percent'], 0.0)
        project.transition(self.root, 'T1', 'run1', 'running')
        project.fail_task(self.root, 'T1', 'run1', 'Repair limit reached')
        snapshot = project.status(self.root)
        self.assertEqual(snapshot['totals']['blocked'], 1)
        self.assertEqual(snapshot['next_tasks'], [])
        self.assertEqual(snapshot['milestones'][0]['status'], 'blocked')
        self.assertIn('Repair limit reached', (self.root / '.kh/task-reports/T1.md').read_text())

    def test_agent_handoffs_report_intermediate_and_accepted_summaries(self):
        self.init(); project.begin_task(self.root, 'T1', 'run1')
        handoffs = [{'role': 'planner', 'summary': 'Planned foundation'},
                    {'role': 'builder', 'summary': 'Implemented foundation'}]
        project.transition(self.root, 'T1', 'run1', 'verifying', {'handoffs': handoffs})
        report_path = self.root / '.kh/task-reports/T1.md'
        self.assertIn('Implemented foundation', report_path.read_text())
        self.assertIn('No accepted result yet.', report_path.read_text())
        project.transition(self.root, 'T1', 'run1', 'reviewing', {'handoffs': handoffs})
        handoffs.append({'role': 'reviewer', 'summary': 'Accepted implementation'})
        project.complete_task(self.root, 'T1', 'run1', self.receipt(handoffs=handoffs))
        self.assertIn('Accepted implementation', report_path.read_text())

    def test_agent_handoff_is_durable_before_any_phase_transition(self):
        self.init(); project.begin_task(self.root, 'T1', 'run1')
        handoff = {'phase': 'plan', 'status': 'success', 'summary': 'Planned the foundation',
                   'artifacts': ['adws/adw_data/sessions/run1/planner/plan.md']}
        returned = project.record_handoff(self.root, 'T1', 'run1', handoff)
        self.assertEqual(returned['status'], 'running')
        self.assertEqual(returned['detail']['handoffs'], [handoff])
        # Use a separately opened SQLite connection: result survived the API transaction.
        with sqlite3.connect(self.root / '.kh/project.db') as conn:
            detail, state = conn.execute('SELECT detail,status FROM tasks WHERE id=?', ('T1',)).fetchone()
            self.assertEqual((json.loads(detail)['handoffs'], state), ([handoff], 'running'))
            event, payload = conn.execute('SELECT event,detail FROM events ORDER BY sequence DESC LIMIT 1').fetchone()
            self.assertEqual((event, json.loads(payload)), ('handoff', handoff))
        self.assertIn('Planned the foundation', (self.root / '.kh/task-reports/T1.md').read_text())
        snapshot = json.loads((self.root / '.kh/project-status.json').read_text())
        self.assertEqual(snapshot['tasks'][0]['detail']['handoffs'], [handoff])
        self.assertEqual(snapshot['totals']['completed'], 0)
        project.transition(self.root, 'T1', 'run1', 'verifying', {'handoffs': [handoff], 'attempt': 1})
        second = {'phase': 'build_1', 'status': 'success', 'summary': '', 'artifacts': []}
        updated = project.record_handoff(self.root, 'T1', 'run1', second)
        self.assertEqual(updated['detail'], {'handoffs': [handoff, second], 'attempt': 1})

    def test_agent_handoff_rejects_stale_inactive_or_invalid_reports(self):
        self.init(); project.begin_task(self.root, 'T1', 'run1')
        handoff = {'phase': 'build', 'status': 'success', 'summary': '', 'artifacts': []}
        for changes in [{'phase': ''}, {'status': ''}, {'summary': None}, {'artifacts': '../escape'},
                        {'artifacts': ['/tmp/report.md']}, {'artifacts': ['../escape']}, {'extra': True}]:
            with self.subTest(changes=changes), self.assertRaises(project.ProjectError):
                project.record_handoff(self.root, 'T1', 'run1', {**handoff, **changes})
        project.recover_task(self.root, 'T1', 'Controller interrupted')
        with self.assertRaisesRegex(project.ProjectError, 'active task'):
            project.record_handoff(self.root, 'T1', 'run1', handoff)
        project.begin_task(self.root, 'T1', 'run2')
        before = project.status(self.root)
        with self.assertRaisesRegex(project.ProjectError, 'Stale'):
            project.record_handoff(self.root, 'T1', 'run1', handoff)
        self.assertEqual(project.status(self.root), before)

    def test_large_finite_weights_do_not_overflow_percentages(self):
        definition = plan()
        for task in definition['tasks']:
            task['weight'] = 1e308
        project.init_project(self.root, definition)
        self.complete()
        self.assertEqual(project.status(self.root)['totals']['completion_percent'], 50.0)
        self.complete('T2', 'run2')
        self.assertEqual(project.status(self.root)['totals']['completion_percent'], 100.0)

    def test_manual_block_and_recover_refuse_live_controller(self):
        self.init(); project.begin_task(self.root, 'T1', 'run1')
        before = project.status(self.root)
        with session.acquire_repo_lock(self.root):
            for call in [lambda: project.recover_task(self.root, 'T1', 'interrupted'),
                         lambda: project.block_task(self.root, 'T2', 'waiting')]:
                with self.assertRaisesRegex(RuntimeError, 'controller'):
                    call()
        self.assertEqual(project.status(self.root), before)
        project.recover_task(self.root, 'T1', 'Interrupted after lock released')
        project.block_task(self.root, 'T2', 'Missing service credentials')
        self.assertEqual(project.status(self.root)['totals']['blocked'], 2)

    def test_receipt_and_ledger_symlinks_are_refused(self):
        outside = self.root.parent / 'outside'; outside.mkdir()
        (self.root / '.kh').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(project.ProjectError):
            self.init()
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / '.kh').unlink(); self.init()
        db = self.root / '.kh/project.db'; moved = outside / 'database'; db.rename(moved); db.symlink_to(moved)
        with self.assertRaises(project.ProjectError):
            project.status(self.root)
        db.unlink(); moved.rename(db)
        self.reviewing(); receipt = self.receipt()
        path = project.receipt_path(self.root, 'T1', 'run1')
        victim = outside / 'receipt.json'; path.rename(victim); path.symlink_to(victim)
        with self.assertRaises(project.ProjectError):
            project.complete_task(self.root, 'T1', 'run1', receipt)

    def test_evidence_parent_symlink_and_database_sidecar_are_refused(self):
        self.init(); self.reviewing(); receipt = self.receipt()
        evidence = self.root / receipt['evidence'][0]
        outside = self.root.parent / 'outside'; outside.mkdir()
        evidence.parent.rename(outside / 'run')
        evidence.parent.symlink_to(outside / 'run', target_is_directory=True)
        with self.assertRaises(project.ProjectError):
            project.complete_task(self.root, 'T1', 'run1', receipt)
        victim = outside / 'victim'; victim.write_text('owner content')
        (self.root / '.kh/project.db-journal').symlink_to(victim)
        with self.assertRaises(project.ProjectError):
            project.status(self.root)
        self.assertEqual(victim.read_text(), 'owner content')

    def test_report_failure_after_acceptance_preserves_database_and_can_rebuild(self):
        self.init(); self.reviewing(); receipt = self.receipt()
        with patch.object(project, '_atomic_text', side_effect=OSError('disk failure')):
            with self.assertWarnsRegex(RuntimeWarning, 'state committed'):
                result = project.complete_task(self.root, 'T1', 'run1', receipt)
        self.assertEqual(result['status'], 'completed')
        self.assertIn('report_error', result)
        self.assertEqual(project.status(self.root)['totals']['completed'], 1)
        rebuilt = project.refresh_reports(self.root)
        self.assertEqual(json.loads((self.root / '.kh/project-status.json').read_text()), rebuilt)

    def test_report_symlink_cannot_overwrite_external_file(self):
        self.init()
        report = self.root / '.kh/task-reports/T1.md'
        victim = self.root.parent / 'victim.md'; victim.write_text('owner document')
        report.unlink(); report.symlink_to(victim)
        with self.assertRaises(project.ProjectError):
            project.refresh_reports(self.root)
        self.assertEqual(victim.read_text(), 'owner document')

    def test_unknown_and_invalid_ids_never_mutate(self):
        self.init(); before = self.files()
        for task_id in ['missing', '../escape', 'T1/other']:
            with self.assertRaises(project.ProjectError):
                project.load_task(self.root, task_id)
            with self.assertRaises(project.ProjectError):
                project.begin_task(self.root, task_id, 'run1')
        self.assertEqual(self.files(), before)

    def test_cli_dry_run_status_and_no_completion_command(self):
        path = self.root / 'plan.json'; path.write_text(json.dumps(plan()))
        def cli(*args):
            return subprocess.run([sys.executable, str(ADWS / 'adw_project.py'), '--root', str(self.root), *args],
                                  text=True, capture_output=True, env=os.environ.copy())
        result = cli('init', '--plan', str(path), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / '.kh').exists())
        result = cli('init', '--plan', str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.files()
        for command in ('status', 'next'):
            result = cli(command)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.files(), before)
        result = cli('complete', '--task', 'T1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(project.status(self.root)['totals']['completed'], 0)

    def test_example_plan_is_valid(self):
        path = ROOT / '.claude/skills/kh/templates/project-plan.example.json'
        self.assertEqual(len(project.validate_plan(json.loads(path.read_text()))['tasks']), 2)


if __name__ == '__main__':
    unittest.main()
