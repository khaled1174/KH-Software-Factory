"""Offline stop reports measure records without dispatching or changing state."""
import json
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote

from tests import test_project as fixture
from adw_modules import project

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/report_interventions.py'


class InterventionReportTests(unittest.TestCase):
    setUp = fixture.ProjectTests.setUp
    tearDown = fixture.ProjectTests.tearDown
    files = fixture.ProjectTests.files

    def init(self, count=1):
        plan = fixture.plan()
        plan['tasks'] = [{**plan['tasks'][0], 'id': f'T{i}'} for i in range(1, count + 1)]
        project.init_project(self.root, plan)

    def stop(self, task, run, reason, recover=False):
        project.begin_task(self.root, task, run)
        if recover:
            project.recover_task(self.root, task, reason)
        else:
            project.fail_task(self.root, task, run, reason)

    def journal(self, run, events):
        path = self.root / 'adws/adw_data/sessions' / run / 'events.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps({'adw_id': run, 'ts': '2026-10-01T10:00:00Z', **e}) + '\n'
                                for e in events), encoding='utf-8')
        return path

    def invoke(self, format='json'):
        # An accidental provider/CLI dispatch must fail even if its output is ignored.
        harness = ('import runpy,sys;'
                   'sys.addaudithook(lambda event,args: '
                   '(_ for _ in ()).throw(RuntimeError("Unexpected dispatch")) '
                   'if event in ("subprocess.Popen","os.system","os.exec","os.posix_spawn") else None);'
                   'sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name="__main__")')
        return subprocess.run([sys.executable, '-B', '-c', harness, str(SCRIPT),
                               '--root', str(self.root), '--format', format],
                              capture_output=True, text=True, timeout=15,
                              env={**os.environ, 'PATH': ''})

    def report(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_measured_stop_categories_and_manual_interventions_stay_distinct(self):
        self.init(7)
        reasons = [
            'OWNER_DECISION_REQUIRED: missing owner record',
            'builder modified prohibited paths: secrets.txt',
            'documenter failed gates after 2 attempt(s)',
            'INFRA_OR_PROVIDER_BLOCKER: unavailable service',
            'Controller terminated by signal',
            'BUDGET_EXHAUSTED: output corrections',
            'A reviewer discusses HTTP 503 and permissions in prose',
        ]
        for i, reason in enumerate(reasons, 1):
            self.stop(f'T{i}', f'r{i}', reason, recover=(i == 5))
        report = self.report()
        self.assertEqual(report['recorded_stops'], 7)
        self.assertEqual(report['counts'], {'owner_decision': 1, 'permissions': 1,
            'quality': 1, 'environment': 1, 'interruption': 1, 'limits': 1, 'unknown': 1})
        self.assertIsNone(report['human_interventions'])
        self.assertEqual(len(report['open_blockers']), 7)
        self.assertEqual(report['provider_calls'], 0)
        self.assertTrue(report['observational_only'])

    def test_missing_history_is_unknown_and_creates_no_state(self):
        before = self.files()
        report = self.report()
        self.assertEqual(report['coverage'], 'unknown')
        self.assertIsNone(report['recorded_stops'])
        self.assertTrue(all(value is None for value in report['counts'].values()))
        self.assertIsNone(report['last_stop_at'])
        self.assertEqual(self.files(), before)

    def test_initialized_unstarted_project_is_distinct_from_missing_history(self):
        self.init()
        report = self.report()
        self.assertEqual(report['coverage'], 'recorded_only')
        self.assertEqual(report['recorded_stops'], 0)
        self.assertEqual(report['open_blockers'], [])

    def test_report_repeats_without_mutating_any_input_or_attempt(self):
        self.init()
        self.stop('T1', 'r1', 'OWNER_REVIEW_REQUIRED: auth: path src/auth.py')
        before = self.files()
        first = self.report()
        self.assertEqual(first, self.report())
        self.assertEqual(self.files(), before)
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'blocked')
        self.assertIn('existing intent', first['open_blockers'][0]['required_action'])
        self.assertNotIn('approve automatically', first['open_blockers'][0]['required_action'])

    def test_journal_and_ledger_do_not_double_count_the_same_stop(self):
        self.init()
        reason = 'documenter failed gates after 2 attempt(s)'
        self.stop('T1', 'r1', reason)
        path = self.journal('r1', [
            {'type': 'gate_fail', 'name': 'proposal_gate', 'phase_id': 'r1_document',
             'payload': {'violations': ['documents: unexpected path docs/wrong.md']}},
            {'type': 'error', 'name': 'document', 'phase_id': 'r1_document',
             'payload': {'error': reason}},
            {'type': 'error', 'name': 'guarded_task', 'phase_id': 'r1_guarded_task',
             'payload': {'error': reason}},
        ])
        before = self.files()
        report = self.report()
        self.assertEqual(report['recorded_stops'], 1)
        stop = report['open_blockers'][0]
        self.assertEqual(stop['phase'], 'document')
        self.assertEqual(stop['gate'], 'proposal_gate')
        self.assertIn('docs/wrong.md', stop['violations'][0])
        self.assertEqual(stop['evidence'][-1]['path'], path.relative_to(self.root).as_posix())
        self.assertEqual(stop['evidence'][-1]['line'], 2)
        self.assertEqual(self.files(), before)

    def test_a_repaired_gate_is_not_a_terminal_stop(self):
        self.journal('r1', [
            {'type': 'gate_fail', 'name': 'proposal_gate', 'payload': {'violations': ['old failure']}},
            {'type': 'error', 'name': 'builder', 'payload': {'error': 'temporary failure'}},
            {'type': 'phase_end', 'name': 'guarded_task', 'payload': {'status': 'success'}},
        ])
        self.assertEqual(self.report()['recorded_stops'], 0)

    def test_missing_ledger_stop_uses_the_journal_once_and_marks_history_partial(self):
        self.init()
        reason = 'documenter failed gates after 2 attempt(s)'
        self.stop('T1', 'r1', reason)
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            conn.execute("DELETE FROM events WHERE event='blocked'")
            conn.commit()
        self.journal('r1', [{'type': 'error', 'name': 'guarded_task',
                            'payload': {'error': reason}}])
        report = self.report()
        self.assertEqual(report['recorded_stops'], 1)
        self.assertEqual(report['coverage'], 'partial')
        self.assertEqual(report['open_blockers'][0]['task_id'], 'T1')
        # Older journal-only history must not hide the newer ledger blocker.
        self.stop('T1', 'r2', 'OWNER_DECISION_REQUIRED: latest decision')
        report = self.report()
        self.assertEqual(report['recorded_stops'], 2)
        self.assertEqual(len(report['open_blockers']), 1)
        self.assertEqual(report['open_blockers'][0]['run_id'], 'r2')

    def test_unrelated_journal_failure_cannot_become_a_stop_gate_or_phase(self):
        self.init()
        self.stop('T1', 'r1', 'OWNER_DECISION_REQUIRED: an owner decision')
        self.journal('r1', [{'type': 'error', 'name': 'guarded_task',
                            'payload': {'error': 'A different failure'}}])
        report = self.report()
        self.assertIsNone(report['open_blockers'][0]['phase'])
        self.assertIsNone(report['open_blockers'][0]['gate'])
        self.assertEqual(report['coverage'], 'partial')

    def test_standalone_terminal_journal_has_no_invented_task_or_current_state(self):
        self.journal('r1', [{'type': 'error', 'name': 'guarded_task',
                            'payload': {'error': 'EXECUTION_DEADLINE: expired'}}])
        report = self.report()
        self.assertEqual(report['recorded_stops'], 1)
        self.assertEqual(report['counts']['limits'], 1)
        self.assertIsNone(report['stops'][0]['task_id'])
        self.assertIsNone(report['stops'][0]['currently_blocked'])
        self.assertIsNone(report['open_blockers'])

    def test_successful_retry_retains_history_without_showing_an_open_blocker(self):
        self.init()
        self.stop('T1', 'first', 'REVIEW_EXISTING_REJECTED: unmet requirement')
        case = fixture.ProjectTests()
        case.root = self.root
        case.complete('T1', 'second')
        report = self.report()
        self.assertEqual(report['recorded_stops'], 1)
        self.assertFalse(report['stops'][0]['currently_blocked'])
        self.assertEqual(report['open_blockers'], [])

    def test_incomplete_blocked_snapshot_is_reported_as_partial_not_silently_zero(self):
        self.init()
        self.stop('T1', 'r1', 'Unclassified failure')
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            conn.execute("DELETE FROM events WHERE event='blocked'")
            conn.commit()
        report = self.report()
        self.assertEqual(report['coverage'], 'partial')
        self.assertEqual(report['recorded_stops'], 1)
        self.assertEqual(report['counts']['unknown'], 1)
        self.assertTrue(report['notes'])

    def test_corrupt_journal_fails_clearly_without_partial_success_or_writes(self):
        path = self.journal('r1', [])
        for content in ('{broken\n', '{"adw_id":"r1","payload":{}}\n',
                        '{"adw_id":"other","type":"error","name":"guarded_task","payload":{}}\n'):
            with self.subTest(content=content):
                path.write_text(content)
                before = self.files()
                result = self.invoke()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('events.jsonl:1', result.stderr)
                self.assertEqual(self.files(), before)

    def test_corrupt_database_is_not_reported_as_no_interventions(self):
        (self.root / '.kh').mkdir()
        (self.root / '.kh/project.db').write_bytes(b'not a database')
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('ledger', result.stderr.lower())

    def test_hot_database_sidecars_are_refused_without_changing_them(self):
        self.init()
        (self.root / '.kh/project.db-wal').write_bytes(b'not a stable offline snapshot')
        before = self.files()
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn('offline', result.stderr.lower())
        self.assertEqual(self.files(), before)

    def test_recorded_reblock_history_has_only_one_current_blocker(self):
        self.init()
        self.stop('T1', 'r1', 'Unknown first blocker')
        project.block_task(self.root, 'T1', 'OWNER_DECISION_REQUIRED: current blocker')
        report = self.report()
        self.assertEqual(report['recorded_stops'], 2)
        self.assertEqual(len(report['open_blockers']), 1)
        self.assertEqual(report['open_blockers'][0]['reason'], 'OWNER_DECISION_REQUIRED: current blocker')

    def test_real_guarded_plan_gate_failure_reports_exact_phase_and_gate_without_changes(self):
        from tests.test_start import StartupTests
        case = StartupTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        result = case.invoke('plan-empty')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        case.unchanged()
        self.root = case.root
        before = self.files()
        report = self.report()
        self.assertEqual(report['recorded_stops'], 1)
        self.assertEqual(report['stops'][0]['category'], 'quality')
        self.assertIsNotNone(report['stops'][0]['gate'])
        self.assertTrue(report['stops'][0]['violations'])
        self.assertEqual(self.files(), before)

    def test_real_successful_repair_does_not_report_a_human_intervention(self):
        from tests.test_start import StartupTests
        case = StartupTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        result = case.invoke('review-repair')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.root = case.root
        report = self.report()
        self.assertEqual(report['recorded_stops'], 0)
        self.assertIsNone(report['human_interventions'])

    def test_symlinked_input_cannot_read_outside_the_selected_project(self):
        with tempfile.TemporaryDirectory() as external:
            secret = Path(external) / 'secret.db'
            secret.write_text('private text')
            (self.root / '.kh').mkdir()
            (self.root / '.kh/project.db').symlink_to(secret)
            result = self.invoke()
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('private text', result.stdout + result.stderr)

    def test_markdown_escapes_recorded_text_and_preserves_required_action_and_evidence(self):
        self.init()
        self.stop('T1', 'r1', 'builder modified prohibited paths: <script>|src/private.py\nnext')
        result = self.invoke('markdown')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Required action', result.stdout)
        self.assertIn('Evidence', result.stdout)
        self.assertIn('.kh/project.db', result.stdout)
        self.assertIn('](<', result.stdout)
        target = re.search(r'\]\(<([^>]+)>\)', result.stdout).group(1)
        self.assertEqual(Path(unquote(target)), self.root / '.kh/project.db')
        self.assertIn('Do not expand permissions', result.stdout)
        self.assertNotIn('<script>', result.stdout)
        self.assertIn('Unknown', result.stdout)


if __name__ == '__main__':
    unittest.main()
