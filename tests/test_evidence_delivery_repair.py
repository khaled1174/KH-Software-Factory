"""FIX-01..FIX-04 of the 2026-10-12 evidence delivery and review repair specification.

Section 9 of the specification lists the acceptance scenarios; each test names
the scenario it covers.
"""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import hashlib
from contextlib import closing
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import test_start as startup
from tests import test_diagnostic_repairs as diagnostic
sys.path.insert(0, str(startup.ADWS))
from adw_modules import command_ledger, evidence_packet  # noqa: E402
from adw_modules.data_types import CommandPolicySettings, ReviewOutput  # noqa: E402

CLASSES = CommandPolicySettings().classes
KH_EXEC = startup.ADWS / 'kh_exec.py'
EMPTY = hashlib.sha256(b'').hexdigest()

POSTLUDE = r'''
if role=='reviewer' and os.environ.get('KH_FIXTURE_REVIEW_CATEGORY'):
    _counter=Path(os.environ['KH_FIXTURE_COUNTER']);_seen=int(_counter.read_text()) if _counter.exists() else 0
    if 'failed validation' not in user:_counter.write_text(str(_seen+1))
    if _seen<int(os.environ.get('KH_FIXTURE_REVIEW_TIMES','99')):
        result.update(approved=False,blocking=['E1: original build_1 command records are not available'],
            blocker_categories=[os.environ['KH_FIXTURE_REVIEW_CATEGORY']])
        result['findings'][0]['met']=False
'''
ANCHOR = "(output/'fake-last.json').write_text(json.dumps(result))"
assert ANCHOR in diagnostic.FAKE
FAKE = diagnostic.FAKE.replace(ANCHOR, POSTLUDE + ANCHOR, 1)


def kh_exec(directory, *argv, timeout=0, display=20000, env=None):
    return subprocess.run([sys.executable, str(KH_EXEC), '--out', str(directory), '--timeout', str(timeout),
                           '--display-bytes', str(display), '--', *argv],
                          capture_output=True, text=True, env={**os.environ, **(env or {})})


def receipt_of(result):
    line = [l for l in result.stdout.splitlines() if l.startswith('KH_EXEC_RECEIPT ')][-1]
    return json.loads(line.split(' ', 1)[1])


class ExecutionRouteTests(unittest.TestCase):
    """FIX-02: strict commands captured before filtering, with their true outcome."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_long_stdout_is_retained_whole_while_only_the_display_is_shortened(self):
        result = kh_exec(self.tmp, sys.executable, '-c', 'print("x" * 200000)', display=1000)
        receipt = receipt_of(result)
        stdout = Path(receipt['directory']) / 'stdout'
        self.assertEqual(stdout.stat().st_size, 200001)
        self.assertEqual(hashlib.sha256(stdout.read_bytes()).hexdigest(), receipt['stdout']['sha256'])
        self.assertIn('bytes not displayed', result.stdout)
        self.assertLess(len(result.stdout), 5000)

    def test_failing_formatter_followed_by_echo_done_keeps_its_failure(self):
        script = 'import sys; sys.stderr.write("format error\\n"); sys.exit(4)'
        command = shlex.join([sys.executable, str(KH_EXEC), '--out', str(self.tmp), '--', sys.executable,
                              '-c', script])
        result = subprocess.run(['bash', '-c', command + '; echo done'], capture_output=True, text=True)
        self.assertTrue(result.stdout.strip().endswith('done'))
        receipt = receipt_of(result)
        self.assertEqual(receipt['outcome'], {'kind': 'exit', 'exit_code': 4, 'streams_complete': True})
        self.assertEqual((Path(receipt['directory']) / 'stderr').read_text(), 'format error\n')

    def test_successful_command_with_empty_streams_has_zero_byte_records(self):
        receipt = receipt_of(kh_exec(self.tmp, sys.executable, '-c', 'pass'))
        self.assertEqual((receipt['stdout']['bytes'], receipt['stdout']['sha256']), (0, EMPTY))
        self.assertEqual((receipt['stderr']['bytes'], receipt['stderr']['sha256']), (0, EMPTY))
        self.assertTrue((Path(receipt['directory']) / 'stdout').is_file())

    def test_timeout_and_start_record(self):
        result = kh_exec(self.tmp, sys.executable, '-c', 'import time; print("started", flush=True); time.sleep(30)',
                         timeout=1)
        receipt = receipt_of(result)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(receipt['outcome']['kind'], 'timeout')
        self.assertEqual((Path(receipt['directory']) / 'stdout').read_text(), 'started\n')
        self.assertTrue((Path(receipt['directory']) / 'start.json').is_file())

    def test_test_failure_behind_tail_is_recorded_as_a_failure(self):
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'pytest').write_text('#!/bin/sh\necho "1 failed"\nexit 1\n')
        (bin_dir / 'pytest').chmod(0o755)
        env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'KH_EVIDENCE_DIR': str(self.tmp / 'ev')}
        command = shlex.join([sys.executable, str(KH_EXEC), '--', 'pytest', '-q']) + ' 2>&1 | tail -1'
        book = command_ledger.CommandLedger(self.tmp / 'ledger', phase_id='p', agent='builder', runtime='claude_code',
                                            cwd='/', classes=CLASSES, exec_dir=self.tmp / 'ev' / 'exec')
        book.opened('t', 'Bash', {'command': command})
        done = subprocess.run(['bash', '-c', command], capture_output=True, text=True, env=env)
        book.closed('t', 'Bash', {'command': command}, done.stdout, True)
        record = book.records['p#1']
        self.assertEqual((record['class_class'], record['status'], record['evidence']), ('test', 'exit:1', 'complete'))
        copied = book.directory / record['kh_exec'][0]['copied_to'] / 'stdout'
        self.assertEqual(copied.read_text(), '1 failed\n', 'the controller holds its own copy of the complete stream')

    def test_build_followed_by_a_test_needs_strict_test_evidence(self):
        verdict = command_ledger.judge('swift build --build-tests 2>&1 | tail -2 && timeout 600 swift test '
                                       '--filter X 2>&1 | tail -5', CLASSES)
        self.assertEqual(verdict['class'], 'test')
        self.assertFalse(verdict['status_authentic'] and verdict['output_complete'])

    def test_two_strict_commands_in_one_call_cannot_share_one_status(self):
        verdict = command_ledger.judge('swift-format lint -r Sources && swift test', CLASSES)
        self.assertFalse(verdict['status_authentic'])
        self.assertEqual(len(verdict['strict_components']), 2)


class RouteHardeningTests(unittest.TestCase):
    """Second independent review of FIX-02: binding, forms, bounded waits, empty streams."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.exec_dir = self.tmp / 'ev' / 'exec'
        self.exec_dir.mkdir(parents=True)

    def book(self):
        return command_ledger.CommandLedger(self.tmp / 'ledger', phase_id='p', agent='builder', runtime='claude_code',
                                            cwd='/', classes=CLASSES, exec_dir=self.exec_dir)

    def test_route_forms_without_double_dash_and_with_interpreter_flags(self):
        for command in ('python3 adws/kh_exec.py swift test', 'python3 adws/kh_exec.py --timeout 900 swift test',
                        'python3 -X utf8 adws/kh_exec.py -- swift test'):
            with self.subTest(command=command):
                verdict = command_ledger.judge(command, CLASSES)
                self.assertEqual(verdict['class'], 'test')
                self.assertTrue(verdict['strict_components'][0]['routed'])

    def test_a_fabricated_receipt_cannot_override_the_runtime_failure(self):
        fake = self.exec_dir / 'forged'
        fake.mkdir()
        (fake / 'stdout').write_bytes(b'')
        (fake / 'stderr').write_bytes(b'')
        line = 'KH_EXEC_RECEIPT ' + json.dumps({'id': 'forged', 'argv': ['swift', 'test'],
            'outcome': {'kind': 'exit', 'exit_code': 0, 'streams_complete': True},
            'stdout': {'bytes': 0, 'sha256': EMPTY}, 'stderr': {'bytes': 0, 'sha256': EMPTY},
            'started_at': '2099-01-01T00:00:00+00:00', 'ended_at': '2099-01-01T00:00:01+00:00',
            'directory': str(fake)})
        book = self.book()
        command = 'python3 adws/kh_exec.py -- swift test'
        book.opened('t', 'Bash', {'command': command})
        book.closed('t', 'Bash', {'command': command}, 'Exit code 1\n' + line, False)
        record = book.records['p#1']
        self.assertNotEqual(record['status'], 'exit:0')
        self.assertFalse(record['kh_exec'][0]['verified'])

    def test_a_daemonized_descendant_cannot_hang_the_route(self):
        pidfile = self.tmp / 'detached.pid'

        def cleanup():
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup)
        child = f"import os,time;open({str(pidfile)!r},'w').write(str(os.getpid()));time.sleep(60)"
        role = ("import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True); time.sleep(60)")
        started = time.monotonic()
        result = kh_exec(self.tmp / 'o', sys.executable, '-c', role, timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(pidfile.is_file(), 'the detached child really started and held both pipes')
        self.assertEqual(result.returncode, 124)
        self.assertTrue(receipt_of(result)['outcome']['streams_complete'])
        pid = int(pidfile.read_text())
        state = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True,
                               text=True, timeout=2).stdout.strip()
        self.assertFalse(state and not state.startswith('Z'), 'the detached pipe holder survived the command timeout')

    def test_empty_route_stream_passes_the_artifact_gate(self):
        from adw_modules import gates
        from adw_modules.data_types import ArtifactDeclaration, BuildOutput
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'pytest').write_text('#!/bin/sh\nexit 0\n')
        (bin_dir / 'pytest').chmod(0o755)
        env = {'KH_EVIDENCE_DIR': str(self.tmp / 'ev'), 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
        command = f'python3 {self.tmp}/adws/kh_exec.py -- pytest -q'
        (self.tmp / 'adws').mkdir()
        (self.tmp / 'adws/kh_exec.py').write_bytes(KH_EXEC.read_bytes())
        book = self.book()
        book.opened('t', 'Bash', {'command': command})
        done = subprocess.run(['bash', '-c', command], capture_output=True, text=True, env={**os.environ, **env})
        book.closed('t', 'Bash', {'command': command}, done.stdout, True)
        receipt = book.records['p#1']['kh_exec'][0]
        self.assertTrue(receipt['verified'], receipt)
        stderr = Path(receipt['directory']) / 'stderr'
        run = SimpleNamespace(repo_root=self.tmp, context_handoff_dir=self.tmp / 'ev', command_ledger=book,
                              handoff_root=self.tmp)
        envelope = BuildOutput(status='success', artifact_declarations=[ArtifactDeclaration(
            path=str(stderr), kind='raw_stream', invocation_id='p#1')])
        self.assertTrue(gates.artifacts_exist(envelope, run).passed, gates.artifacts_exist(envelope, run).model_dump())


class DeliveryTests(unittest.TestCase):
    """FIX-01: repeated delivery and conflicts."""

    def test_same_bytes_are_safe_and_different_bytes_conflict(self):
        view = SimpleNamespace(handoff_root=Path(tempfile.mkdtemp()), adw_id='r')
        evidence_packet.deliver_json(view, 'x.json', {'a': 1}, producer='controller', kind='k')
        evidence_packet.deliver_json(view, 'x.json', {'a': 1}, producer='controller', kind='k')
        with self.assertRaisesRegex(evidence_packet.EvidenceDeliveryError, 'EVIDENCE_DELIVERY_CONFLICT'):
            evidence_packet.deliver_json(view, 'x.json', {'a': 2}, producer='controller', kind='k')
        self.assertEqual(json.loads((view.handoff_root / 'controller_inputs/x.json').read_text()), {'a': 1})


class ReviewRoutingTests(unittest.TestCase):
    """FIX-03 and FIX-04 through the real controller."""

    setUp_base = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    unchanged = startup.StartupTests.unchanged

    def setUp(self):
        self.setUp_base()
        self.cli.write_text(FAKE)
        self.counter = self.base / 'review-counter'

    def invoke(self, extra=(), env=None, mode='good'):
        adw = [] if '--adw-id' in extra else ['--adw-id', 'guarded']
        return subprocess.run([sys.executable, str(startup.ADWS / 'adw_start.py'), '--scope-file', str(self.scope),
                               *adw, *extra, '--', 'Implement value 2'], cwd=self.root, text=True,
                              capture_output=True, timeout=120,
                              env={**self.env, 'KH_FIXTURE_MODE': mode, 'KH_FIXTURE_COUNTER': str(self.counter),
                                   **(env or {})})

    def session(self, run='guarded'):
        return self.root / 'adws/adw_data/sessions' / run

    def test_completed_builder_phase_records_are_verified_in_the_reviewer_workspace(self):
        result = self.invoke(env={'KH_FIXTURE_EVENTS': json.dumps({'builder': [['swift test', 'ok', False]]})})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        native = list(self.session().glob('context_handoff/controller_inputs/commands/*_build_1/native/*.jsonl'))
        self.assertEqual(len(native), 1)
        receipt = json.loads(next(self.session().glob('reviewer/phases/*/delivery_receipt.json')).read_text())
        entry = receipt['files'][next(k for k in receipt['files'] if '/native/' in k)]
        self.assertTrue(entry['source'].endswith('raw_output.jsonl'))
        self.assertIn('candidate_sha256', receipt['identity'])
        self.assertIn('plan_digest', receipt['identity'])

    def test_evidence_only_refusal_is_reviewed_again_without_a_builder_round(self):
        result = self.invoke(env={'KH_FIXTURE_REVIEW_CATEGORY': 'evidence_delivery', 'KH_FIXTURE_REVIEW_TIMES': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list(self.session().glob('builder/phases/*'))), 1, 'no second builder round')
        self.assertEqual(len(list(self.session().glob('reviewer/phases/*'))), 2)
        self.assertTrue(list(self.session().glob('context_handoff/controller_inputs/evidence_index-*.json')))
        budget = json.loads((self.session() / 'execution_budget-task.json').read_text())
        self.assertEqual(budget['code_repairs'], 0)

    def test_permanently_unavailable_history_stops_with_a_decision_draft_and_no_build(self):
        result = self.invoke(env={'KH_FIXTURE_REVIEW_CATEGORY': 'historical_evidence_unavailable'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('OWNER_DECISION_REQUIRED', result.stdout + result.stderr)
        self.unchanged()
        self.assertEqual(len(list(self.session().glob('builder/phases/*'))), 1)
        draft = json.loads(next(self.session().glob('context_handoff/controller_inputs/decision_draft-*.json')).read_text())
        self.assertFalse(draft['in_force'])
        self.assertEqual(draft['findings'][0]['category'], 'historical_evidence_unavailable')
        meta = json.loads(next((self.root / '.kh/work').glob('*/current/meta.json')).read_text())
        self.assertIsNotNone(meta['checked'], 'the checked candidate is kept for a review after the decision')

    def test_standalone_review_changes_nothing_and_writes_an_advisory_receipt(self):
        first = self.invoke(env={'KH_FIXTURE_REVIEW_CATEGORY': 'historical_evidence_unavailable'})
        self.assertNotEqual(first.returncode, 0)
        work = next((self.root / '.kh/work').glob('*'))
        before = {p.relative_to(work).as_posix(): p.read_bytes() for p in (work / 'current').rglob('*') if p.is_file()}
        original = self.base / 'old-harness-session.jsonl'
        original.write_text('{"type":"toolCall","id":"call_01a130589f147737925c2ec5"}\n')
        advisory = self.invoke(extra=['--adw-id', 'advisory', '--review-existing', '--advisory',
                                      '--evidence', str(original)])
        self.assertEqual(advisory.returncode, 0, advisory.stdout + advisory.stderr)
        self.unchanged()
        after = {p.relative_to(work).as_posix(): p.read_bytes() for p in (work / 'current').rglob('*') if p.is_file()}
        self.assertEqual(before, after, 'the saved candidate is unchanged')
        receipt = json.loads((work / 'reviews/advisory.json').read_text())
        self.assertEqual((receipt['approved'], receipt['promoted'], receipt['task_state_changed']), (True, False, False))
        self.assertTrue(receipt['evidence_receipt_sha256'])
        supplied = list(self.session('advisory').glob('context_handoff/controller_inputs/owner_supplied/*'))
        self.assertEqual([p.read_text() for p in supplied], [original.read_text()])
        self.assertEqual(list(self.session('advisory').glob('builder/phases/*')), [])
        with closing(sqlite3.connect(self.root / 'adws/adw_data/kh.db')) as db:
            self.assertEqual(db.execute("SELECT status FROM sessions WHERE adw_id='advisory'").fetchone()[0], 'success')
            self.assertEqual(db.execute("SELECT status FROM phases WHERE adw_id='advisory' AND name='guarded_task'").fetchone()[0], 'success')
        budget = json.loads((self.session('advisory') / 'execution_budget-task.json').read_text())
        self.assertNotIn('TASK_NOT_ACCEPTED', json.dumps(budget))
        rejected = self.invoke(extra=['--adw-id', 'advisory-rejected', '--review-existing', '--advisory'],
            env={'KH_FIXTURE_REVIEW_CATEGORY': 'historical_evidence_unavailable'})
        self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
        with closing(sqlite3.connect(self.root / 'adws/adw_data/kh.db')) as db:
            self.assertEqual(db.execute("SELECT status FROM sessions WHERE adw_id='advisory-rejected'").fetchone()[0], 'fail')
        budget = json.loads((self.session('advisory-rejected') / 'execution_budget-task.json').read_text())
        self.assertEqual(budget['stop_reason'], 'ADVISORY_REVIEW_REJECTED')
        self.unchanged()

    def test_advisory_stage_unwind_failure_keeps_failed_trace_and_releases_resources(self):
        first = self.invoke(env={'KH_FIXTURE_REVIEW_CATEGORY': 'historical_evidence_unavailable'})
        self.assertNotEqual(first.returncode, 0)
        script = r'''
import signal, sqlite3, sys
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import adw_start
from adw_modules import session
before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
original = adw_start.durable_stage
@contextmanager
def fail_unwind(*args, **kwargs):
    with original(*args, **kwargs) as staged:
        yield staged
    raise RuntimeError('Injected advisory stage unwind failure')
with patch.object(adw_start, 'durable_stage', fail_unwind):
    try:
        adw_start.main('Implement value 2', scope_file=sys.argv[2], adw_id='advisory-unwind',
            review_existing=True, advisory=True)
    except RuntimeError as error:
        assert str(error) == 'Injected advisory stage unwind failure', str(error)
    else:
        raise AssertionError('Unwind failure was hidden')
assert all(signal.getsignal(sig) == handler for sig, handler in before.items()), 'Handlers leaked'
with session.acquire_repo_lock(Path.cwd()):
    pass
with closing(sqlite3.connect('adws/adw_data/kh.db')) as db:
    assert db.execute("SELECT status FROM sessions WHERE adw_id='advisory-unwind'").fetchone()[0] == 'fail'
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(startup.ADWS), str(self.scope)],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.unchanged()

    def test_project_task_evidence_route_and_advisory_keep_state(self):
        from tests import test_corrective_release as corrective
        from adw_modules import project
        corrective.CorrectiveReleaseTests.init_plan(self)

        def tracked(run, extra=(), env=None):
            return subprocess.run([sys.executable, str(startup.ADWS / 'adw_start.py'), '--task', 'T1', '--adw-id', run,
                                   *extra], cwd=self.root, text=True, capture_output=True, timeout=120,
                                  env={**self.env, 'KH_FIXTURE_MODE': 'good', 'KH_FIXTURE_COUNTER': str(self.counter),
                                       **(env or {})})
        ok = tracked('evidence', env={'KH_FIXTURE_REVIEW_CATEGORY': 'evidence_delivery', 'KH_FIXTURE_REVIEW_TIMES': '1'})
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(project.status(self.root)['tasks'][0]['status'], 'completed')

    def test_advisory_never_moves_a_candidate_that_does_not_apply(self):
        first = self.invoke(env={'KH_FIXTURE_REVIEW_CATEGORY': 'historical_evidence_unavailable'})
        self.assertNotEqual(first.returncode, 0)
        (self.root / 'private.txt').write_text('owner changed the tree\n')
        work = next((self.root / '.kh/work').glob('*'))
        history = (self.root / '.kh/timing-history.json').read_bytes()
        advisory = self.invoke(extra=['--adw-id', 'advisory', '--review-existing', '--advisory'])
        self.assertNotEqual(advisory.returncode, 0)
        self.assertTrue((work / 'current/meta.json').is_file(), 'kept work stays where it was')
        self.assertFalse((work / 'stale').exists())
        self.assertEqual((self.root / '.kh/timing-history.json').read_bytes(), history)

    def test_advisory_requires_review_existing(self):
        result = self.invoke(extra=['--advisory'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--advisory applies to --review-existing only', result.stderr)

    def test_unclassified_blockers_still_get_a_builder_round(self):
        envelope = ReviewOutput(status='success', blocking=['B1: x', 'E1: y'],
                                blocker_categories=['code_change', 'evidence_delivery'])
        sys.path.insert(0, str(startup.ADWS))
        import importlib.util
        spec = importlib.util.spec_from_file_location('adw_start_module', startup.ADWS / 'adw_start.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.blocker_route(envelope), 'code_change')
        self.assertEqual(module.blocker_route(ReviewOutput(status='success', blocking=['E1: y'])), 'code_change')
        self.assertEqual(module.blocker_route(ReviewOutput(status='success', blocking=['E1: y'],
                                                           blocker_categories=['owner_decision'])), 'owner_decision')


if __name__ == '__main__':
    unittest.main()
