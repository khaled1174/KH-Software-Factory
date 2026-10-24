"""Controller acceptance examples for evidence, plans, cleanup and recovery.

Unit tests exercise each module directly; the integration tests drive the real
controller (adw_start.py) through the deterministic fake Claude Code CLI of
test_start, extended to emit real stream-json tool events.
"""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import test_start as startup
sys.path.insert(0, str(startup.ADWS))
from adw_modules import (command_ledger, dispositions, evidence_packet, findings, gates, infrastructure,  # noqa: E402
                         resume, timing, permissions)
from adw_modules.data_types import (ArtifactDeclaration, BuildOutput, CommandPolicySettings, KHConfig,  # noqa: E402
                                    ReviewFinding, ReviewOutput)
from adw_modules.agents import gate_summary  # noqa: E402

CLASSES = CommandPolicySettings().classes

PRELUDE = r'''
_ev=json.loads(os.environ.get('KH_FIXTURE_EVENTS','{}'))
_key=role+('_correction' if 'failed validation' in user else '')
for _i,_item in enumerate(_ev.get(_key,[])):
    _cmd,_out,_err=_item
    _id=f'{_key}-{_i}-{os.getpid()}'
    print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':_id,'name':'Bash','input':{'command':_cmd}}]}}),flush=True)
    print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':_id,'content':_out,'is_error':_err}]}}),flush=True)
(output/'seen-prompt.txt').write_text(user)
if role=='reviewer' and os.environ.get('KH_FIXTURE_REVIEW_CRASH')=='1':
    print('killed by the role timeout',file=sys.stderr);raise SystemExit(124)
'''
ANCHOR = "result={'status':'success','summary':role,'artifacts':[str(evidence)]}\n"
assert ANCHOR in startup.FAKE
FAKE = startup.FAKE.replace(ANCHOR, ANCHOR + PRELUDE, 1)


def ledger(tmp, **kw):
    return command_ledger.CommandLedger(Path(tmp) / 'ledger', phase_id='run_09_build_1', agent='builder',
                                        runtime='claude_code', cwd='/w', classes=CLASSES, **kw)


# ── SYS-01: command records at the tool boundary ─────────────────────────────

class CommandLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_failed_test_followed_by_tail_cannot_acquire_a_successful_status(self):
        book = ledger(self.tmp.name)
        command = 'swift test 2>&1 | tail -20; EXIT=$?; echo "EXIT=$EXIT"'
        book.opened('t1', 'Bash', {'command': command})
        book.closed('t1', 'Bash', {'command': command}, 'Test Suite failed\nEXIT=0', True)
        record = book.records['run_09_build_1#1']
        self.assertEqual(record['class_class'], 'test')
        self.assertEqual(record['status'], 'unknown')
        self.assertEqual(record['evidence'], 'excerpt')
        self.assertFalse(command_ledger.complete(record))

    def test_find_xargs_pipeline_has_one_full_record(self):
        book = ledger(self.tmp.name)
        command = 'find Sources -name "*.swift" -print0 | xargs -0 swift-format lint --strict'
        book.opened('t1', 'Bash', {'command': command})
        book.closed('t1', 'Bash', {'command': command}, '', True)
        record = book.records['run_09_build_1#1']
        self.assertEqual(record['command'], command)
        self.assertEqual(record['class_class'], 'format')
        self.assertEqual(record['status'], 'exit:0')
        self.assertTrue(command_ledger.complete(record))

    def test_zero_byte_stream_is_retained_and_authenticated(self):
        book = ledger(self.tmp.name)
        book.opened('t1', 'Bash', {'command': 'curl -sf http://localhost:9/health'})
        book.closed('t1', 'Bash', {'command': 'curl -sf http://localhost:9/health'}, 'Exit code 7\n', False)
        book.opened('t2', 'Bash', {'command': 'pg_isready -q'})
        book.closed('t2', 'Bash', {'command': 'pg_isready -q'}, '', True)
        empty = book.records['run_09_build_1#2']['stream']
        self.assertEqual((empty['bytes'], empty['sha256'], empty['empty']), (0, command_ledger.EMPTY_SHA256, True))
        self.assertEqual((book.directory / empty['path']).read_bytes(), b'')
        self.assertEqual(book.records['run_09_build_1#1']['status'], 'exit:7')

    def test_interrupted_command_leaves_a_durable_unknown_record(self):
        book = ledger(self.tmp.name)
        book.opened('t1', 'Bash', {'command': 'swift test'})
        book.finalize()
        reloaded = ledger(self.tmp.name)
        record = reloaded.records['run_09_build_1#1']
        self.assertEqual(record['status'], 'interrupted')
        self.assertIsNone(record['runtime_exit_code'])
        self.assertEqual(command_ledger.gaps(reloaded.invocations())[0]['why'], 'interrupted')

    def test_two_commands_never_share_a_capture_name_and_the_ledger_is_chained(self):
        book = ledger(self.tmp.name)
        for call in ('a', 'b'):
            book.opened(call, 'Bash', {'command': 'swift test'})
            book.closed(call, 'Bash', {'command': 'swift test'}, f'output {call}', True)
        streams = {r['stream']['path'] for r in book.invocations()}
        self.assertEqual(len(streams), 2)
        lines = book.path.read_bytes().splitlines()
        for previous, current in zip(lines, lines[1:]):
            self.assertEqual(json.loads(current)['prev_sha256'], hashlib.sha256(previous).hexdigest())

    def test_exploratory_excerpt_is_an_excerpt_never_passing_evidence(self):
        book = ledger(self.tmp.name)
        command = 'swift build 2>&1 | grep -E "error|warning" | head -40'
        book.opened('t1', 'Bash', {'command': command})
        book.closed('t1', 'Bash', {'command': command}, 'error: x', True)
        row = command_ledger.coverage(book.invocations())[0]
        self.assertEqual((row['class'], row['strict'], row['evidence'], row['requirement']),
                         ('exploratory_compile', False, 'excerpt', 'metadata'))
        self.assertNotIn('complete_output', row['establishes'])
        self.assertNotIn('exit_status', row['establishes'])

    def test_formatting_command_without_complete_record_is_named_before_review(self):
        book = ledger(self.tmp.name)
        run = SimpleNamespace(command_ledger=book, cfg=KHConfig(), owner_decisions=None)
        command = 'swift-format lint -r Sources | tail -5'
        book.opened('t1', 'Bash', {'command': command})
        book.closed('t1', 'Bash', {'command': command}, 'ok', True)
        report = command_ledger.gate(None, run)
        self.assertFalse(report.passed)
        self.assertIn('run_09_build_1#1', report.violations[0])
        self.assertIn('format', report.violations[0])
        book.opened('t2', 'Bash', {'command': 'swift-format lint -r Sources'})
        book.closed('t2', 'Bash', {'command': 'swift-format lint -r Sources'}, '', True)
        self.assertTrue(command_ledger.gate(None, run).passed)
        rows = command_ledger.coverage(book.invocations())
        self.assertEqual(rows[0]['resolution'], 'superseded')
        self.assertEqual(rows[0]['superseded_by'], 'run_09_build_1#2')
        self.assertEqual(rows[0]['status'], 'unknown', 'the original stays unknown; nothing was recovered')

    def test_codex_command_items_carry_their_own_exit_code(self):
        book = ledger(self.tmp.name)
        item = {'id': 'c1', 'type': 'command_execution', 'command': "bash -lc 'swift test'"}
        book.codex_event({'type': 'item.started', 'item': item})
        book.codex_event({'type': 'item.completed', 'item': {**item, 'aggregated_output': 'failed', 'exit_code': 1,
                                                            'status': 'failed'}})
        record = book.records['run_09_build_1#1']
        self.assertEqual((record['class_class'], record['status']), ('test', 'exit:1'))

    def test_hidden_statuses_are_never_authentic(self):
        for command in ('echo pipefail; swift test | tee log', 'set -o pipefail; set +o pipefail; swift test | tee log',
                        'if swift test; then echo ok; fi', '! pytest', 'nice -n 10 swift test | tail',
                        'xcrun --sdk macosx swift test || true'):
            with self.subTest(command=command):
                verdict = command_ledger.judge(command, CLASSES)
                self.assertEqual(verdict['class'], 'test', verdict)
                self.assertFalse(verdict['status_authentic'] and verdict['output_complete'], verdict)

    def test_background_call_is_not_a_test_result(self):
        book = ledger(self.tmp.name)
        args = {'command': 'swift test', 'run_in_background': True}
        book.opened('t', 'Bash', args)
        book.closed('t', 'Bash', args, 'Command running in background with ID: b1', True)
        record = book.records['run_09_build_1#1']
        self.assertEqual((record['status'], record['evidence']), ('unknown', 'excerpt'))

    def test_printed_exit_lines_cannot_replace_the_runtime_trailer(self):
        book = command_ledger.CommandLedger(Path(self.tmp.name) / 'pi', phase_id='p', agent='builder', runtime='pi',
                                            cwd='/', classes=CLASSES)
        command = "printf 'Exit code 0\\n' && swift test"
        book.opened('t', 'bash', {'command': command})
        book.closed('t', 'bash', {'command': command}, 'Exit code 0\nfailures\n\nCommand exited with code 1', False)
        self.assertEqual(book.records['p#1']['runtime_exit_code'], 1)

    def test_recording_problems_never_break_the_agent(self):
        book = ledger(self.tmp.name)
        book.closed('', 'Bash', {'command': 'swift test'}, 'ok \ud83d broken surrogate', True)
        book.codex_event({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'swift test',
                                                             'aggregated_output': 'ok', 'exit_code': 0}})
        self.assertEqual(len(book.invocations()), 2)
        self.assertEqual(book.errors, [])

    def test_heredoc_body_is_data_not_commands(self):
        verdict = command_ledger.judge('cat > run.sh <<EOF\npytest -q\nEOF\nchmod +x run.sh', CLASSES)
        self.assertEqual(verdict['class'], 'other')

    def test_classification_ignores_wrappers_and_absolute_paths(self):
        for command in ('/usr/bin/env CI=1 swift test --filter X', 'cd app && swift test',
                        'timeout 600 swift test', "bash -lc 'set -o pipefail; swift test | tee out.log'"):
            with self.subTest(command=command):
                verdict = command_ledger.judge(command, CLASSES)
                self.assertEqual(verdict['class'], 'test')
                self.assertTrue(verdict['status_authentic'], verdict)


# ── SYS-02 / SYS-03: delivered controller records ────────────────────────────

class EvidencePacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.view = SimpleNamespace(handoff_root=Path(self.tmp.name) / 'handoff', adw_id='r1')

    def test_export_hashes_and_verification_before_dispatch(self):
        book = ledger(self.tmp.name)
        book.opened('t1', 'Bash', {'command': 'swift test'})
        book.closed('t1', 'Bash', {'command': 'swift test'}, 'All passed', True)
        summary = evidence_packet.export_commands(self.view, 'run_09_build_1', 'builder', book)
        self.assertEqual((summary['delivery'], summary['invocations'], summary['gaps']), ('exported', 1, 0))
        evidence_packet.verify(self.view)
        stream = self.view.handoff_root / 'controller_inputs/commands/run_09_build_1/streams/run_09_build_1_1.out'
        self.assertEqual(stream.read_text(), 'All passed')
        receipts = sorted((self.view.handoff_root / 'controller_inputs/receipts').glob('*.json'))
        self.assertTrue(receipts)
        self.assertIn('controller_inputs/commands/run_09_build_1/ledger.jsonl', json.loads(receipts[-1].read_text())['files'])
        stream.write_text('All passed (edited)')
        with self.assertRaisesRegex(evidence_packet.EvidenceDeliveryError, 'EVIDENCE_DELIVERY_ERROR'):
            evidence_packet.verify(self.view)
        stream.unlink()
        with self.assertRaisesRegex(evidence_packet.EvidenceDeliveryError, 'missing'):
            evidence_packet.verify(self.view)

    def test_a_phase_without_tool_events_is_reported_unavailable(self):
        summary = evidence_packet.export_commands(self.view, 'run_05_build_1', 'builder', None)
        self.assertEqual(summary['delivery'], 'unavailable')
        self.assertIn('no tool events', summary['reason'])


# ── SYS-04: deadline against permitted work ──────────────────────────────────

class TimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = SimpleNamespace(max_repairs=3, deadline_seconds=10800)

    def seed(self):
        for kind, seconds in (('planning', 900), ('build', 1200), ('checks', 300), ('review', 1400)):
            timing.record(self.root, kind, seconds)

    def test_four_cycle_workload_under_an_incompatible_limit_has_a_policy_outcome(self):
        self.seed()
        result = timing.forecast(self.root, self.policy, lenses=1)
        self.assertFalse(result['supported'])
        self.assertEqual(result['forecast_seconds'], 900 + 4 * (1200 + 300 + 1400))
        self.assertEqual(result['cycles_that_fit'], 3)
        cfg = KHConfig.model_validate({'execution': {'timing_preflight': 'refuse'}})
        with self.assertRaisesRegex(ValueError, 'TIMING_PREFLIGHT'):
            timing.preflight(self.root, cfg, self.policy, 1)
        warn = timing.preflight(self.root, KHConfig(), self.policy, 1)
        self.assertEqual((warn['mode'], warn['supported']), ('warn', False))

    def test_no_history_never_guesses_and_modes_are_kept_apart(self):
        result = timing.forecast(self.root, self.policy, lenses=1)
        self.assertIsNone(result['supported'])
        self.seed()
        light = SimpleNamespace(max_repairs=1, deadline_seconds=5400, mode='light')
        self.assertIsNone(timing.forecast(self.root, light, lenses=1)['supported'])

    def test_a_review_that_cannot_finish_is_not_started(self):
        budget = SimpleNamespace(policy=SimpleNamespace(deadline_seconds=10800),
                                 started_monotonic=time.monotonic() - 10225)
        run = SimpleNamespace(execution_budget=budget, cfg=KHConfig())
        clock = timing.Clock(self.root)
        timing.require(run, clock, [('review', 1)], 'CANDIDATE_CHECKED_UNREVIEWED', 'review')  # nothing measured yet
        for seconds in (1300, 1530, 1400):
            clock.done('review', seconds)
        with self.assertRaisesRegex(timing.InsufficientTime, 'CANDIDATE_CHECKED_UNREVIEWED'):
            timing.require(run, clock, [('review', 1)], 'CANDIDATE_CHECKED_UNREVIEWED', 'review')
        budget.started_monotonic = time.monotonic()
        timing.require(run, clock, [('review', 1)], 'CANDIDATE_CHECKED_UNREVIEWED', 'review')

    def test_deadline_stop_is_classified_apart_from_a_rejection(self):
        self.assertEqual(resume.failure_kind('EXECUTION_DEADLINE_EXCEEDED'), 'deadline')
        self.assertEqual(resume.failure_kind('CANDIDATE_CHECKED_UNREVIEWED: 500s remain'), 'deadline')
        self.assertEqual(resume.failure_kind('Task was not accepted within the configured repair budget; x'),
                         'review_rejected')


# ── SYS-05: infrastructure capabilities ──────────────────────────────────────

class InfrastructureTests(unittest.TestCase):
    def tearDown(self):
        infrastructure.reset()

    def configure(self, **settings):
        cfg = KHConfig.model_validate({'execution': {'infrastructure': settings}})
        infrastructure.configure(cfg, 'Run-42')
        return cfg

    def test_worker_environment_withholds_overrides_and_shims(self):
        self.configure(withhold_env=['ADMIN_DATABASE_URL'], worker_env={'DATABASE_URL': '${TEST_URL}'},
                       resource_prefix='kh_', deny_executables=['docker'])
        env = infrastructure.apply_worker_env({'ADMIN_DATABASE_URL': 'super', 'TEST_URL': 'limited',
                                               'PATH': '/usr/bin'})
        self.assertNotIn('ADMIN_DATABASE_URL', env)
        self.assertEqual(env['DATABASE_URL'], 'limited')
        self.assertEqual(env['KH_RESOURCE_PREFIX'], 'kh_run_42_')
        done = subprocess.run(['docker', 'restart', 'pg'], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 126)
        self.assertIn(command_ledger.DENIED_MARKER, done.stderr)
        self.assertIn('Bash(docker:*)', infrastructure.claude_deny_rules())

    def test_executed_forbidden_command_is_a_breach_even_by_absolute_path(self):
        self.configure()
        breaches = []
        book = command_ledger.CommandLedger(Path(tempfile.mkdtemp()) / 'l', phase_id='p', agent='builder',
                                            runtime='claude_code', cwd='/', classes=CLASSES,
                                            forbidden=infrastructure.forbidden_patterns(), on_forbidden=breaches.append,
                                            inspect=infrastructure.inspect)
        for i, command in enumerate(('/usr/local/bin/docker restart shared-pg',
                                     'psql "$DATABASE_URL" -c "ALTER SYSTEM SET max_connections = 300"')):
            book.opened(f't{i}', 'Bash', {'command': command})
            book.closed(f't{i}', 'Bash', {'command': command}, 'shared-pg', True)
        self.assertEqual(len(breaches), 2)
        with self.assertRaisesRegex(infrastructure.InfrastructureBreach, 'INFRA_BOUNDARY_VIOLATION'):
            infrastructure.check_record(breaches[0])
        self.assertIn('does not claim whether the server', str(self._raises(breaches[1])))

    def _raises(self, record):
        try:
            infrastructure.check_record(record)
        except infrastructure.InfrastructureBreach as error:
            return error
        self.fail('no breach')

    def test_refused_attempt_is_recorded_not_a_breach(self):
        self.configure()
        breaches = []
        book = command_ledger.CommandLedger(Path(tempfile.mkdtemp()) / 'l', phase_id='p', agent='builder',
                                            runtime='claude_code', cwd='/', classes=CLASSES,
                                            forbidden=infrastructure.forbidden_patterns(), on_forbidden=breaches.append,
                                            inspect=infrastructure.inspect)
        book.opened('t', 'Bash', {'command': 'docker restart pg'})
        book.closed('t', 'Bash', {'command': 'docker restart pg'},
                    f'{command_ledger.DENIED_MARKER}: docker is not available', False)
        self.assertEqual(breaches[0]['status'], 'refused')
        infrastructure.check_record(breaches[0])
        fallback = 'docker restart pg || /usr/local/bin/docker restart pg'
        book.opened('t2', 'Bash', {'command': fallback})
        book.closed('t2', 'Bash', {'command': fallback}, f'{command_ledger.DENIED_MARKER}: refused\npg', True)
        self.assertNotEqual(breaches[1]['status'], 'refused', 'one refused stage cannot hide an executed one')
        with self.assertRaises(infrastructure.InfrastructureBreach):
            infrastructure.check_record(breaches[1])

    def test_read_only_commands_are_not_infrastructure_control(self):
        self.configure()
        for command in ('cat docker/start.sh', 'pytest tests/docker -k restart', 'docker run --rm img echo hi',
                        'git diff -- infra/docker/rm-volumes.md', 'sed -n 1,80p src/service/stop.ts',
                        'docker compose up -d --wait postgres', 'docker ps', 'npm start'):
            with self.subTest(command=command):
                self.assertEqual(infrastructure.inspect(command), [])
        for command in ('/usr/local/bin/docker restart db', 'docker compose down -v', 'sudo systemctl restart postgresql',
                        'pg_ctl -D data stop'):
            with self.subTest(command=command):
                self.assertTrue(infrastructure.inspect(command))

    def test_drop_outside_the_run_resources_is_forbidden(self):
        self.configure(resource_prefix='kh_')
        patterns = [__import__('re').compile(p) for p in infrastructure.forbidden_patterns()]
        self.assertTrue(any(p.search('psql -c "DROP SCHEMA public CASCADE"') for p in patterns))
        self.assertFalse(any(p.search('psql -c "DROP SCHEMA IF EXISTS kh_run_42_t1 CASCADE"') for p in patterns))

    def test_capability_probe_refuses_an_unrestricted_environment(self):
        cfg = KHConfig.model_validate({'execution': {'infrastructure': {'capability_checks': [
            {'name': 'not-superuser', 'argv': [sys.executable, '-c', 'print("t")'], 'expect_stdout': 'f'}]}}})
        with tempfile.TemporaryDirectory() as session:
            with self.assertRaisesRegex(RuntimeError, 'INFRA_CAPABILITY_NOT_RESTRICTED'):
                infrastructure.run_capability_checks(cfg, dict(os.environ), Path(session))
            cfg.execution.infrastructure.capability_checks[0].argv[-1] = 'print("f")'
            self.assertTrue(infrastructure.run_capability_checks(cfg, dict(os.environ), Path(session))[0]['passed'])


# ── SYS-06: resume metadata ──────────────────────────────────────────────────

class ResumeMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'src').mkdir()
        (self.root / 'src/app.py').write_text('value = 2\n')
        self.view = SimpleNamespace(repo_root=self.root, cfg=KHConfig(), snapshot_excludes=['.kh'],
                                    context_handoff_dir=self.root / 'handoff')

    def save(self, **kw):
        return resume.save(self.root, 'F2A', run_id=kw.pop('run_id', 'r1'), reason=kw.pop('reason', 'stopped'),
                           owner_digest='o', plan=None, plan_content='p', view=self.view, changed=['src/app.py'],
                           excluded=['.kh'], **kw)

    def test_a_later_stop_without_a_review_keeps_the_last_real_review(self):
        review = {'run_id': 'r1', 'phase': 'review_2', 'approved': False, 'summary': '6 of 8 met',
                  'blocking': ['B1: x', 'E1: three exploratory compilations lack records'], 'candidate_sha256': 'c'}
        self.save(last_review=review, reason='Task was not accepted within the configured repair budget')
        meta = self.save(run_id='r2', reason='Changes exceed task scope: Tests/TestSupport.swift')
        self.assertEqual(meta['last_review']['run_id'], 'r1')
        self.assertEqual(meta['last_failure'], {'run_id': 'r2', 'kind': 'scope',
                                                'message': 'Changes exceed task scope: Tests/TestSupport.swift'})

    def test_refused_paths_are_named_with_their_baseline(self):
        baseline = permissions.snapshot(SimpleNamespace(repo_root=self.root, cfg=KHConfig(), snapshot_excludes=['.kh']))
        identity = resume.baseline_identity(baseline, 'src/app.py')
        meta = self.save(rejected=[{'path': 'src/app.py', 'reason': 'Changes exceed task scope', 'baseline': identity}])
        text = resume.brief(meta, None)
        self.assertIn('REFUSED PATHS', text)
        self.assertIn('NOT kept', text)
        self.assertIn(identity['sha256'], text)
        self.assertIn('existed before this task', text)
        self.assertEqual(resume.baseline_identity(baseline, 'new.py'), {'state': 'absent from the owner baseline'})

    def test_long_review_findings_are_not_silently_truncated(self):
        blocking = [f'B{i}: ' + 'detail ' * 200 for i in range(1, 9)]
        meta = self.save(last_review={'run_id': 'r1', 'phase': 'review_1', 'approved': False, 'summary': 's',
                                      'blocking': blocking}, feedback='f' * 9000)
        text = resume.brief(meta, None)
        for item in blocking:
            self.assertIn(item.strip(), text)
        self.assertEqual(len(meta['feedback']), 9000)

    def test_work_saved_by_340_still_resumes(self):
        self.save()
        meta_path = resume.work_dir(self.root, 'F2A') / 'current/meta.json'
        meta = json.loads(meta_path.read_text())
        meta['schema'] = 1
        meta_path.write_text(json.dumps(meta))
        stage = self.root / 'stage'
        stage.mkdir()
        kept, _ = resume.apply(self.root, 'F2A', owner_digest='o', plan_content='p', stage_root=stage, excluded=[])
        self.assertIsNotNone(kept)
        self.assertEqual((stage / 'src/app.py').read_text(), 'value = 2\n')


# ── SYS-07: artifact typing, path base, reporting ────────────────────────────

class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = self.root / 'adws/adw_data/sessions/r/context_handoff/r_09_build_1/builder'
        self.evidence.mkdir(parents=True)
        self.book = ledger(self.root / 'session')
        self.run = SimpleNamespace(repo_root=self.root, context_handoff_dir=self.evidence, command_ledger=self.book,
                                   handoff_root=self.root / 'adws/adw_data/sessions/r/context_handoff')

    def report(self, envelope):
        return gates.artifacts_exist(envelope, self.run)

    def test_silent_exit_1_probe_is_valid_with_its_invocation_record(self):
        self.book.opened('t', 'Bash', {'command': 'pg_isready -q'})
        self.book.closed('t', 'Bash', {'command': 'pg_isready -q'}, 'Exit code 1\n', False)
        stream = self.book.records['run_09_build_1#1']
        self.assertEqual(stream['status'], 'exit:1')
        silent = ledger(self.root / 'other')
        silent.opened('t', 'Bash', {'command': 'pg_isready -q'})
        silent.closed('t', 'Bash', {'command': 'pg_isready -q'}, '', False)
        self.run.command_ledger = silent
        empty = self.evidence / 'probe.stdout'
        empty.write_bytes(b'')
        envelope = BuildOutput(status='success', artifacts=[str(empty)], artifact_declarations=[
            ArtifactDeclaration(path=str(empty), kind='raw_stream', invocation_id='run_09_build_1#1')])
        self.assertTrue(self.report(envelope).passed, self.report(envelope).model_dump())
        undeclared = BuildOutput(status='success', artifacts=[str(empty)])
        self.assertFalse(self.report(undeclared).passed)
        report = BuildOutput(status='success', artifact_declarations=[ArtifactDeclaration(path=str(empty), kind='report')])
        self.assertFalse(self.report(report).passed)

    def test_missing_files_and_bare_names_get_actionable_errors(self):
        (self.evidence / 'notes.md').write_text('content')
        result = self.report(BuildOutput(status='success', artifacts=['notes.md']))
        self.assertFalse(result.passed)
        self.assertIn('not your evidence directory', result.violations[0])
        self.assertIn(str(self.evidence / 'notes.md'), result.violations[0])
        declared = BuildOutput(status='success', artifacts=[], artifact_declarations=[
            ArtifactDeclaration(path='notes.md', base='evidence')])
        self.assertTrue(self.report(declared).passed)
        missing = self.report(BuildOutput(status='success', artifacts=['gone.md']))
        self.assertIn('a bare file name resolves to', missing.violations[0])

    def test_final_reporting_shows_failed_attempt_then_passed_attempt(self):
        rows = gate_summary([{'attempt': 1, 'gate': 'artifacts_exist', 'ok': False, 'violations': ['x']},
                             {'attempt': 2, 'gate': 'artifacts_exist', 'ok': True, 'violations': []}])
        self.assertEqual(rows[0]['text'], 'artifacts_exist: failed attempt 1; passed attempt 2')

    def test_cumulative_and_phase_change_sets_are_distinct_claims(self):
        (self.root / 'a.py').write_text('a')
        view = SimpleNamespace(repo_root=self.root, cfg=KHConfig(), snapshot_excludes=['adws'])
        task = permissions.snapshot(view)
        (self.root / 'a.py').write_text('a2')
        phase = permissions.snapshot(view)
        (self.root / 'b.py').write_text('b')
        run = SimpleNamespace(repo_root=self.root, cfg=KHConfig(), snapshot_excludes=['adws'], agent_baseline=phase,
                              task_baseline=task, agent_output_relative='adws/out')
        ok = gates.diff_matches_claims(BuildOutput(status='success', changed_files=['b.py'],
                                                   cumulative_task_changed_files=['a.py', 'b.py']), run)
        self.assertTrue(ok.passed, ok.model_dump())
        mixed = gates.diff_matches_claims(BuildOutput(status='success', changed_files=['a.py', 'b.py'],
                                                      cumulative_task_changed_files=['b.py']), run)
        self.assertEqual(len(mixed.violations), 2)


# ── SYS-08: stable finding identities ────────────────────────────────────────

class FindingIdentityTests(unittest.TestCase):
    def test_lens_numbering_and_short_tags_never_merge_distinct_findings(self):
        registry = findings.Registry()
        result = findings.carry_forward(['req A'], ['[security] B1: SQL injection in login',
            '[correctness] B1: off-by-one in pager', 'E2E: login flow untested', 'E2E: logout flow untested',
            'MD5 - weak hash', 'B4: x', 'B4: y'], 1, registry)
        self.assertEqual(len(result), 8)
        self.assertIn('security/B1', registry.findings)
        self.assertIn('correctness/B1', registry.findings)

    def test_rewording_b3_revises_it_and_b3_d2_is_distinct(self):
        registry = findings.Registry()
        first = findings.carry_forward(['Original need'], ['B3: release the gate on setup failure'], 1, registry)
        second = findings.carry_forward(first, ['B3 — release, cancel and rejoin on setup failure',
                                                    'B3-D2: the releaser task is never joined'], 2, registry)
        ids = [findings.finding_id(r) for r in second if findings.finding_id(r)]
        self.assertEqual(ids, ['B3', 'B3-D2'])
        self.assertEqual([r['text'] for r in registry.findings['B3']['revisions']],
                         ['release the gate on setup failure', 'release, cancel and rejoin on setup failure'])

    def test_full_correction_text_is_kept(self):
        registry = findings.Registry()
        text = 'E1: ' + 'exact command and timing record missing; ' * 300
        findings.carry_forward([], [text], 1, registry)
        self.assertEqual(registry.findings['E1']['revisions'][-1]['text'], text[4:].strip())

    def test_states_and_stable_denominator(self):
        registry = findings.Registry()
        required = findings.carry_forward(['R1 need', 'R2 need'], ['B1: a', 'B2: b'], 1, registry)
        registry.rule([ReviewFinding(requirement=findings.line('B1', 'a'), met=True, evidence='x'),
                       ReviewFinding(requirement=findings.line('B2', 'b'), met=False, evidence='y'),
                       ReviewFinding(requirement='R1 need', met=True, evidence='z')], 2, False)
        progress = findings.progress(registry, required,
                                     [ReviewFinding(requirement='R1 need', met=True, evidence='z')])
        self.assertEqual(progress['original_requirements'], {'total': 2, 'met_in_last_review': 1})
        self.assertEqual(progress['findings'], {'open': 1, 'resolved': 1, 'regressed': 0, 'total': 2})
        findings.carry_forward(required, ['B1: a came back'], 3, registry)
        self.assertEqual(registry.findings['B1']['state'], 'regressed')

    def test_restated_finding_is_covered_by_id(self):
        registry = findings.Registry()
        required = findings.carry_forward(['need'], ['B3: release the gate'], 1, registry)
        run = SimpleNamespace(required_requirements=required, scope_review_paths=None)
        envelope = ReviewOutput(status='success', approved=True, artifacts=['r.md'], findings=[
            ReviewFinding(requirement='need', met=True, evidence='a:1'),
            ReviewFinding(requirement='Prior review blocker B3 is resolved: gate released at T.swift:40', met=True,
                          evidence='T.swift:40')])
        self.assertTrue(gates.verdict_consistent(envelope, run).passed)

    def test_legacy_carried_lines_are_migrated_not_lost(self):
        legacy = ("Prior review blocker R1.2 is resolved in the current code (met only if fixed, cited by "
                  "file:line, or no longer applicable with the reason; a builder's claim is not evidence): Fix Z")
        registry = findings.Registry()
        merged = registry.merge_requirements(['need', legacy])
        self.assertIn('R1.2', registry.findings)
        self.assertEqual(merged, ['need', legacy])


# ── SYS-09: owner dispositions as records ────────────────────────────────────

class DispositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.kh').mkdir()

    def write(self, decisions):
        (self.root / '.kh/dispositions.json').write_text(json.dumps({'schema_version': 1, 'decisions': decisions}))

    def decision(self, **kw):
        return {'id': 'T3-X1', 'task': 'F2A', 'kind': 'historical_exception', 'authority': 'Fixture owner',
                'recorded_at': '2026-10-12T15:00:00Z', 'invocations': ['54d5#109'], 'classes': ['format'],
                'substitution': 'controller format check of the final candidate',
                'does_not_establish': 'the original exit status', **kw}

    def test_exceptions_match_exact_invocations_classes_and_earlier_executions_only(self):
        self.write([self.decision()])
        decisions = dispositions.load(self.root, 'F2A', [c.name for c in CLASSES])
        record = {'invocation_id': '54d5#109', 'class_class': 'format', 'started_at': '2026-10-11T10:00:00+00:00'}
        self.assertEqual(dispositions.match(decisions, record)['id'], 'T3-X1')
        self.assertIsNone(dispositions.match(decisions, {**record, 'invocation_id': '54d5#110'}))
        self.assertIsNone(dispositions.match(decisions, {**record, 'class_class': 'exploratory_compile'}))
        self.assertIsNone(dispositions.match(decisions, {**record, 'started_at': '2026-10-13T10:00:00+00:00'}),
                          'a historical exception never waives a later execution')
        self.assertEqual(dispositions.load(self.root, 'F3', []), [])

    def test_patterns_and_incomplete_records_are_refused(self):
        for bad in (self.decision(invocations=['54d5#*']), self.decision(authority=''),
                    self.decision(id='lowercase'), self.decision(classes=[])):
            with self.subTest(bad=bad):
                self.write([bad])
                with self.assertRaises(dispositions.DispositionError):
                    dispositions.load(self.root, 'F2A', [c.name for c in CLASSES])

    def test_a_gap_is_reconciled_by_its_decision_record(self):
        self.write([self.decision(invocations=['run_09_build_1#1'])])
        decisions = dispositions.load(self.root, 'F2A', [c.name for c in CLASSES])
        book = ledger(self.tmp.name)
        book.opened('t', 'Bash', {'command': 'swift-format lint . | tail'})
        book.closed('t', 'Bash', {'command': 'swift-format lint . | tail'}, 'x', True)
        record = book.records['run_09_build_1#1']
        record['started_at'] = '2026-10-11T10:00:00+00:00'
        rows = command_ledger.coverage(book.invocations(), decisions)
        self.assertEqual((rows[0]['resolution'], rows[0]['decision']), ('disposition', 'T3-X1'))

    def test_output_cannot_claim_an_unrecorded_exception(self):
        run = SimpleNamespace(owner_decisions=[self.decision()], findings_registry=None)
        claim = BuildOutput(status='success', summary='Formatting gap covered by owner exception T3-X9.')
        self.assertFalse(dispositions.claims_gate(claim, run).passed)
        stretched = BuildOutput(status='success', summary='Per owner exception T3-X1, 54d5#111 needs no record.')
        self.assertIn('does not name', dispositions.claims_gate(stretched, run).violations[0])
        fine = BuildOutput(status='success', summary='Owner exception T3-X1 covers 54d5#109 only.')
        self.assertTrue(dispositions.claims_gate(fine, run).passed)
        for prose in ('Handled the exception e.g. TimeoutError.', 'Design decision: re-run the probe.',
                      'Kept the decision (pre-existing behaviour).', 'The exception (non-fatal) is logged.',
                      'Raised exception: ValueError.message is clear.'):
            with self.subTest(prose=prose):
                self.assertTrue(dispositions.claims_gate(BuildOutput(status='success', summary=prose), run).passed)

    def test_policy_digest_is_stable_and_rendered(self):
        cfg = KHConfig()
        first = dispositions.policy_record(cfg, [self.decision()])
        self.assertEqual(first['digest'], dispositions.policy_record(cfg, [self.decision()])['digest'])
        self.assertIn(first['digest'], dispositions.instruction(first, 'p.json'))
        self.assertNotEqual(first['digest'], dispositions.policy_record(cfg, [])['digest'])


# ── Plan coverage derived from steps ─────────────────────

class PlanCoverageTests(unittest.TestCase):
    def setUp(self):
        from adw_modules import planning, scope
        self.planning = planning
        self.contract = scope.ScopeContract(original_request='Implement value 2', deliverables=['Update value'],
                                            allowed_paths=['src/app.py'])
        self.ids = list(planning.requirements(self.contract))

    def plan(self, steps, coverage):
        from adw_modules.planning import ReviewedPlanOutput
        return ReviewedPlanOutput.model_validate({
            'status': 'success', 'summary': 'plan', 'acceptance_criteria': ['value is 2'], 'allowed_paths': ['src/app.py'],
            'steps': steps, 'requirement_coverage': coverage, 'required_checks': []})

    def step(self, ident, reqs, depends=()):
        return {'id': ident, 'action': 'do', 'paths': ['src/app.py'], 'requirement_ids': reqs,
                'validation': ['run the test'], 'depends_on': list(depends)}

    def test_a_stale_coverage_map_is_derived_not_fatal(self):
        plan = self.plan([self.step('S1', self.ids), self.step('S2', self.ids[:1], ['S1'])],
                         {r: ['S1', 'S9'] for r in self.ids})
        normalized, stated = self.planning.normalize_plan(plan, self.contract)
        self.assertIsNotNone(stated)
        self.planning.validate_plan(normalized, self.contract)
        self.assertEqual(normalized.requirement_coverage[self.ids[0]], ['S1', 'S2'])

    def test_real_defects_still_fail(self):
        uncovered = self.plan([self.step('S1', self.ids[:1])], {})
        normalized, _ = self.planning.normalize_plan(uncovered, self.contract)
        with self.assertRaisesRegex(self.planning.PlanningBlocked, f'Requirement {self.ids[1]} is covered by no step'):
            self.planning.validate_plan(normalized, self.contract)
        unknown = self.plan([self.step('S1', [*self.ids, 'R99'])], {})
        normalized, _ = self.planning.normalize_plan(unknown, self.contract)
        with self.assertRaises(self.planning.PlanningBlocked):
            self.planning.validate_plan(normalized, self.contract)
        bad_order = self.plan([self.step('S1', self.ids, ['S2']), self.step('S2', self.ids)], {})
        normalized, _ = self.planning.normalize_plan(bad_order, self.contract)
        with self.assertRaises(self.planning.PlanningBlocked):
            self.planning.validate_plan(normalized, self.contract)


# ── Integration: the real controller ─────────────────────────────────────────

class ControllerIntegrationTests(unittest.TestCase):
    setUp_base = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    unchanged = startup.StartupTests.unchanged

    def setUp(self):
        self.setUp_base()
        self.cli.write_text(FAKE)

    def invoke(self, mode='good', extra=(), events=None, env=None):
        environment = {**self.env, 'KH_FIXTURE_MODE': mode, 'KH_FIXTURE_EVENTS': json.dumps(events or {}),
                       **(env or {})}
        adw = [] if '--adw-id' in extra else ['--adw-id', 'guarded']
        return subprocess.run([sys.executable, str(startup.ADWS / 'adw_start.py'), '--scope-file', str(self.scope),
                               *adw, *extra, '--', 'Implement value 2'], cwd=self.root, env=environment,
                              text=True, capture_output=True, timeout=120)

    def session(self, run='guarded'):
        return self.root / 'adws/adw_data/sessions' / run

    def prompt(self, role, run='guarded'):
        found = sorted(self.session(run).rglob(f'{role}/phases/*/attempt-001/user.md'))
        self.assertTrue(found, f'no {role} prompt in {run}')
        return found[-1].read_text()

    def test_builder_commands_are_recorded_and_delivered_to_the_reviewer(self):
        result = self.invoke(events={'builder': [['swift test', 'Executed 12 tests, with 0 failures', False]]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ledgers = list(self.session().glob('commands/*_build_1/ledger.jsonl'))
        self.assertEqual(len(ledgers), 1)
        records = [json.loads(line) for line in ledgers[0].read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['start', 'end'])
        self.assertEqual(records[1]['status'], 'exit:0')
        reviewer = self.prompt('reviewer')
        self.assertIn('controller_inputs/commands', reviewer)
        delivered = list(self.session().glob('context_handoff/controller_inputs/commands/*_build_1/streams/*.out'))
        self.assertEqual([p.read_text() for p in delivered], ['Executed 12 tests, with 0 failures'])
        # SYS-03: the complete approved plan is a delivered controller record, not a builder transcription.
        plan = self.session() / 'context_handoff/controller_inputs/plan'
        self.assertEqual((plan / 'plan.json').read_bytes(), (self.session() / 'planning/plan-1.json').read_bytes())
        provenance = json.loads((plan / 'provenance.json').read_text())
        self.assertTrue(provenance['plan_digest_verified'])
        self.assertTrue(provenance['coverage_verified'])
        self.assertIn('never plan provenance', provenance['request_digest_note'])
        self.assertIn('Authoritative plan records', self.prompt('builder'))
        policy = json.loads((self.session() / 'context_handoff/controller_inputs/policy.json').read_text())
        self.assertIn(policy['digest'], reviewer)

    def test_timing_preflight_refuses_before_any_model_dispatch(self):
        self.cfg['execution'] = {'timing_preflight': 'refuse'}
        self.config.write_text(json.dumps(self.cfg))
        for kind in ('planning', 'build', 'checks', 'review'):
            timing.record(self.root, kind, 100000)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('TIMING_PREFLIGHT', result.stderr)
        self.assertFalse(self.session().exists())
        self.unchanged()

    def test_unrecorded_owner_exception_is_rejected_before_promotion(self):
        fake = self.cli.read_text()
        self.assertIn("'summary':role", fake)
        self.cli.write_text(fake.replace("'summary':role",
            "'summary':('Owner exception T3-X9 covers the change.' if role=='builder' else role)"))
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('T3-X9', result.stdout + result.stderr)
        self.unchanged()

    def test_masked_test_status_is_named_in_session_and_closed_by_a_complete_rerun(self):
        result = self.invoke(events={
            'builder': [['swift test 2>&1 | tail -5; echo "EXIT=$?"', 'FAILED\nEXIT=0', False]],
            'builder_correction': [['swift test', 'Executed 12 tests, with 0 failures', False]]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        coverage = json.loads(next(self.session().glob(
            'context_handoff/controller_inputs/commands/*_build_1/coverage.json')).read_text())
        self.assertEqual([(r['status'], r['resolution']) for r in coverage],
                         [('unknown', 'superseded'), ('exit:0', 'complete')])
        attempts = json.loads(next(self.session().glob('builder/phases/*_build_1/gate_history.json')).read_text())
        self.assertIn('gate: failed attempt 1; passed attempt 2', [row['text'] for row in attempts])

    def test_executed_infrastructure_command_stops_the_run_and_changes_nothing(self):
        result = self.invoke(events={'builder': [['/usr/local/bin/docker restart shared-pg', 'shared-pg', False]]})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('INFRA_BOUNDARY_VIOLATION', result.stdout + result.stderr)
        self.unchanged()

    def test_interrupted_review_then_review_only_continuation_promotes_without_a_builder(self):
        first = self.invoke(env={'KH_FIXTURE_REVIEW_CRASH': '1'})
        self.assertNotEqual(first.returncode, 0)
        self.unchanged()
        meta = json.loads((self.root / '.kh/work').glob('*/current/meta.json').__next__().read_text())
        self.assertIsNotNone(meta['checked'], 'a checked candidate whose review was cut off is kept as checked')
        self.assertIsNone(meta['last_review'], 'an interrupted review is never a verdict')
        second = self.invoke(extra=['--adw-id', 'reviewonly', '--review-existing'])
        output = second.stdout + second.stderr
        self.assertEqual(second.returncode, 0, output)
        self.assertIn('reused the approved plan', output)
        self.assertEqual(list(self.session('reviewonly').glob('builder/phases/*')), [], 'no builder round')
        checks = list(self.session('reviewonly').rglob('quality/reused_test/command.log'))
        self.assertTrue(checks, 'unchanged candidate reuses its recorded checks')
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')

    def invalid_cached_check_runs_current_commands(self, defect):
        counter = self.base / 'lint-executions.txt'
        self.cfg['defaults']['quality_checks']['lint'] = [sys.executable, '-c',
            f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text((p.read_text() if p.exists() else '')+'lint\\n')"]
        self.cfg['defaults']['required_quality_checks'] = ['test', 'lint']
        self.config.write_text(json.dumps(self.cfg))
        first = self.invoke(env={'KH_FIXTURE_REVIEW_CRASH': '1'})
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(counter.read_text(), 'lint\n')
        meta_path = next((self.root / '.kh/work').glob('*/current/meta.json'))
        meta = json.loads(meta_path.read_text())
        checks = meta['checked']['verification_result']['checks']
        if defect == 'missing':
            checks[:] = [check for check in checks if check['name'] != 'lint']
        elif defect == 'argv':
            next(check for check in checks if check['name'] == 'lint')['command'] = 'python -c pass'
        else:
            checks.append({**next(check for check in checks if check['name'] == 'lint'), 'name': 'build'})
            directory = meta_path.parent / 'checks'
            (directory / 'build.log').write_bytes((directory / 'lint.log').read_bytes())
        meta_path.write_text(json.dumps(meta))
        second = self.invoke(extra=['--adw-id', 'reviewonly', '--review-existing'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(counter.read_text(), 'lint\nlint\n', 'Current lint must execute instead of trusting incomplete or different cached commands')
        fresh = list(self.session('reviewonly').glob('context_handoff/quality/*_lint/command.log'))
        self.assertTrue(fresh, 'Current lint needs its own fresh command log')
        self.assertFalse(list(self.session('reviewonly').rglob('quality/reused_test/command.log')))

    def test_review_only_reruns_checks_when_required_lint_is_missing_from_cache(self):
        self.invalid_cached_check_runs_current_commands('missing')

    def test_review_only_reruns_checks_when_cached_argv_differs(self):
        self.invalid_cached_check_runs_current_commands('argv')

    def test_review_only_reruns_checks_when_cached_names_include_unrequired_check(self):
        self.invalid_cached_check_runs_current_commands('extra')

    def test_setup_failure_closes_trace_restores_handlers_and_removes_shim(self):
        self.cfg['execution'] = {'infrastructure': {'enabled': True, 'deny_executables': ['docker']}}
        self.config.write_text(json.dumps(self.cfg))
        observations = self.base / 'setup-observations.json'
        temporary = self.base / 'shim-temp'
        temporary.mkdir()
        script = r'''
import json, signal, sqlite3, sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import adw_start
from adw_modules import infrastructure, session
before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
state = {}
def fail_after_shim(*args, **kwargs):
    state['shim'] = str(infrastructure._shim_dir)
    state['shim_created'] = infrastructure._shim_dir.is_dir()
    raise RuntimeError('Injected post-configuration setup failure')
try:
    with patch.object(adw_start.timing, 'Clock', side_effect=fail_after_shim):
        adw_start.main('Implement value 2', scope_file=sys.argv[2], adw_id='setup-failure')
except RuntimeError as error:
    state['error'] = str(error)
state['handlers_restored'] = all(signal.getsignal(sig) == handler for sig, handler in before.items())
state['shim_removed'] = not Path(state['shim']).exists()
try:
    with session.acquire_repo_lock(Path.cwd()):
        state['lock_released'] = True
except RuntimeError:
    state['lock_released'] = False
with closing(sqlite3.connect('adws/adw_data/kh.db')) as db:
    state['status'] = db.execute("SELECT status FROM sessions WHERE adw_id='setup-failure'").fetchone()[0]
    state['agent_processes'] = db.execute("SELECT COUNT(*) FROM processes WHERE kind='agent'").fetchone()[0]
Path(sys.argv[3]).write_text(json.dumps(state))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(startup.ADWS), str(self.scope),
            str(observations)], cwd=self.root, env={**self.env, 'TMPDIR': str(temporary)},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(observations.read_text())
        self.assertEqual(state['error'], 'Injected post-configuration setup failure')
        self.assertTrue(state['shim_created'])
        for key in ('handlers_restored', 'shim_removed', 'lock_released'):
            with self.subTest(key=key):
                self.assertTrue(state[key], state)
        self.assertEqual(state['status'], 'fail', state)
        self.assertEqual(state['agent_processes'], 0)
        self.unchanged()

    def test_review_only_continuation_cannot_promote_a_rejected_candidate(self):
        first = self.invoke('semantic', env={'KH_FIXTURE_REVIEW_CRASH': '1'})
        self.assertNotEqual(first.returncode, 0)
        second = self.invoke('semantic', extra=['--adw-id', 'reviewonly', '--review-existing'])
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('REVIEW_EXISTING_REJECTED', second.stdout + second.stderr)
        self.unchanged()
        meta = json.loads(next((self.root / '.kh/work').glob('*/current/meta.json')).read_text())
        self.assertEqual(meta['last_review']['run_id'], 'reviewonly')
        self.assertFalse(meta['last_review']['approved'])

    def test_tracked_task_review_only_continuation_completes_with_a_normal_receipt(self):
        from tests import test_corrective_release as corrective
        from adw_modules import project
        corrective.CorrectiveReleaseTests.init_plan(self)

        def tracked(run, extra=(), env=None):
            return subprocess.run([sys.executable, str(startup.ADWS / 'adw_start.py'), '--task', 'T1', '--adw-id', run,
                                   *extra], cwd=self.root, text=True, capture_output=True, timeout=120,
                                  env={**self.env, 'KH_FIXTURE_MODE': 'good', **(env or {})})
        first = tracked('tracked', env={'KH_FIXTURE_REVIEW_CRASH': '1'})
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(project.status(self.root)['tasks'][0]['status'], 'blocked')
        second = tracked('again', extra=['--review-existing'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        task = project.status(self.root)['tasks'][0]
        self.assertEqual(task['status'], 'completed')
        receipt = json.loads(next((self.root / '.kh/task-receipts').rglob('*.json')).read_text())
        self.assertTrue(receipt['review_existing'])
        self.assertTrue(receipt['review_approved'])
        self.assertTrue(all(c['reused_from_run'] == 'tracked' for c in receipt['checks']))
        self.assertIn('digest', receipt['evidence_policy'])
        self.assertEqual(receipt['findings']['findings']['open'], 0)

    def test_review_only_without_a_checked_candidate_is_refused(self):
        result = self.invoke(extra=['--review-existing'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('REVIEW_EXISTING', result.stdout + result.stderr)
        self.unchanged()

    def test_refused_path_is_named_to_the_next_builder(self):
        first = self.invoke('crash-unauthorized')
        self.assertNotEqual(first.returncode, 0)
        meta = json.loads(next((self.root / '.kh/work').glob('*/current/meta.json')).read_text())
        self.assertEqual([r['path'] for r in meta['rejected']], ['private.txt'])
        self.assertEqual(meta['rejected'][0]['baseline']['state'], 'present in the owner baseline')
        self.assertEqual(meta['paths'], [], 'a phase that broke its boundary contributes nothing')

    def test_a_scope_stop_after_a_rejection_keeps_its_findings_for_the_next_review(self):
        self.cfg['routing']['profiles']['current']['max_repairs'] = 0
        self.config.write_text(json.dumps(self.cfg))
        first = self.invoke('review-repair')
        self.assertNotEqual(first.returncode, 0)
        second = self.invoke('crash-build', extra=['--adw-id', 'second'])
        self.assertNotEqual(second.returncode, 0)
        meta = json.loads(next((self.root / '.kh/work').glob('*/current/meta.json')).read_text())
        self.assertEqual(meta['last_review']['run_id'], 'guarded')
        self.assertEqual(meta['last_failure']['run_id'], 'second')
        third = self.invoke(extra=['--adw-id', 'third'])
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        self.assertIn('Prior review blocker R1.1 is resolved', self.prompt('reviewer', 'third'))
        self.assertIn('LAST INDEPENDENT REVIEW (run guarded', self.prompt('builder', 'third'))


if __name__ == '__main__':
    unittest.main()
