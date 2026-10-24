"""An unconfirmed role stop cannot promote work, retry, or delete its CWD."""
import gc
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADWS = ROOT / '.claude/skills/kh/templates/adws'
sys.path.insert(0, str(ADWS))
from tests import test_core as core
from adw_modules import agents, agent_cc, permissions, process_io, transient, workspace
from adw_modules.data_types import UsageBreakdown


class RoleCleanupBoundaryTests(unittest.TestCase):
    def call(self, *args, **kwargs):
        # A broken cleanup boundary may retry; reproduce that failure without
        # making a regression test spend production's outage backoff minutes.
        with patch.object(transient, 'delay', return_value=0):
            return core.CoreTests.call(self, *args, **kwargs)

    def setUp(self):
        core.CoreTests.setUp(self)
        self.run.salvage_failed_build = True
        self.children = []
        self.worker_roots = set()
        self.policy_directories = {}

    def tearDown(self):
        try:
            # These are the exact Popen objects and kernel birth identities
            # created by this fixture; a reused PID never becomes ours.
            for child, birth in self.children:
                if process_io.process_identity(child.pid) == birth:
                    try:
                        os.kill(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait(timeout=5)
            for directory, identity in self.policy_directories.items():
                try:
                    info = directory.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                    shutil.rmtree(directory)
            for root in self.worker_roots:
                if root.parent.name.startswith('kh-agent-'):
                    shutil.rmtree(root.parent, ignore_errors=True)
        finally:
            core.CoreTests.tearDown(self)

    def remember(self, child):
        birth = process_io.process_identity(child.pid)
        self.assertIsNotNone(birth)
        self.children.append((child, birth))
        return child, birth

    def living_child(self, root):
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        return self.remember(child)

    @staticmethod
    def partial():
        return agent_cc.CCRunResult(tokens=10, cost=1.25, returncode=124, timed_out=True,
            usage=UsageBreakdown(input_tokens=4, output_tokens=6, total_tokens=10, total_cost=1.25))

    def assert_retained(self, error, root):
        self.assertTrue(root.is_dir(), 'a potentially live provider still owns this CWD')
        self.assertEqual(Path(error.retained_workspace), root)
        self.assertEqual(Path(self.run.role_cleanup_pending['retained_workspace']), root)
        self.assertFalse(error.kh_cleanup['complete'])
        receipts = list(Path(self.run.context_handoff_dir).glob('role-cleanup-pending/*.json'))
        matching = [json.loads(path.read_text()) for path in receipts
                    if json.loads(path.read_text())['retained_workspace'] == str(root)]
        self.assertEqual(len(matching), 1)
        events = [json.loads(line) for line in (Path(self.run.session_dir) / 'events.jsonl').read_text().splitlines()]
        self.assertTrue(any(e.get('name') == 'role_cleanup_pending' for e in events))
        self.assertFalse(any(e.get('name') in ('build_salvaged', 'transient_retry') for e in events))

    def test_partial_priced_cleanup_error_blocks_salvage_and_later_send(self):
        def failed(request, **_):
            root = Path(request.cwd)
            self.worker_roots.add(root)
            (root / 'source.py').write_text('value = 2\n')
            raise process_io.RoleCleanupError('service unavailable', self.partial(),
                {'complete': False, 'surviving_pids': [999999], 'root_reaped': False})
        with patch.object(workspace.AgentWorkspace, 'salvage', wraps=workspace.AgentWorkspace.salvage) as salvage:
            with self.assertRaises(process_io.RoleCleanupError) as caught:
                self.call(failed)
            salvage.assert_not_called()
        root, = self.worker_roots
        self.assertEqual((self.run.tokens, self.run.cost, self.run.cost_known), (10, 1.25, False))
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 1\n')
        self.assertFalse(caught.exception.resume_safe)
        self.assert_retained(caught.exception, root)
        later_sends = []
        def must_not_send(*args, **kwargs):
            later_sends.append(args)
            raise AssertionError('known live role must block another provider call')
        with self.assertRaises(RuntimeError) as blocked:
            self.call(must_not_send, name='must_not_restart')
        self.assertEqual(later_sends, [])
        self.assertTrue(process_io.cleanup_incomplete(blocked.exception))

    def test_incomplete_legacy_timeout_cannot_become_recoverable(self):
        def failed(request, **_):
            root = Path(request.cwd)
            self.worker_roots.add(root)
            (root / 'source.py').write_text('value = 2\n')
            error = process_io.RoleTimeout('supervisor timeout', self.partial())
            error.kh_cleanup = {'complete': False, 'surviving_pids': [999999]}
            raise error
        with self.assertRaises(process_io.RoleTimeout) as caught:
            self.call(failed)
        self.assertFalse(caught.exception.resume_safe, 'guarded-task recovery requires resume_safe')
        self.assertEqual(caught.exception.salvaged_paths, [])
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 1\n')
        self.assert_retained(caught.exception, next(iter(self.worker_roots)))

    def test_callback_failure_survives_permission_exception_and_baseexception(self):
        for kind in (RuntimeError, SystemExit):
            with self.subTest(kind=kind.__name__):
                def failed(request, **_):
                    root = Path(request.cwd)
                    self.worker_roots.add(root)
                    self.latest_worker = root
                    child, birth = self.living_child(root)
                    self.latest_child = (child, birth)
                    (root / 'owner.txt').write_text('forbidden')
                    error = kind('503 service unavailable in callback')
                    error.kh_cleanup = {'complete': False, 'surviving_pids': [child.pid], 'root_reaped': False}
                    raise error
                with self.assertRaises(permissions.PermissionBreach) as caught:
                    self.call(failed, name='masked_' + kind.__name__)
                self.assertTrue(process_io.cleanup_incomplete(caught.exception))
                self.assertEqual(process_io.process_identity(self.latest_child[0].pid), self.latest_child[1])
                self.assertIsNone(self.latest_child[0].poll())
                self.assert_retained(caught.exception, self.latest_worker)
                self.assertFalse((self.root / 'owner.txt').exists())
                # Resolve only this subcase's exact owned child before removing
                # its receipt. Clearing a memory flag alone must never resume.
                child, birth = self.latest_child
                if process_io.process_identity(child.pid) == birth:
                    child.kill()
                child.wait(timeout=5)
                for receipt in (Path(self.run.context_handoff_dir) / 'role-cleanup-pending').glob('*.json'):
                    if json.loads(receipt.read_text()).get('retained_workspace') == str(self.latest_worker):
                        receipt.unlink()
                del self.run.role_cleanup_pending

    def test_workspace_retention_disarms_gc_and_import_paths(self):
        phase = SimpleNamespace(phase_id='manual', params=SimpleNamespace(owner='builder'))
        isolated = workspace.AgentWorkspace(self.run, phase, self.agent)
        root = isolated.root
        self.worker_roots.add(root)
        child, birth = self.living_child(root)
        isolated.view.agent_baseline = permissions.snapshot(isolated.view)
        (root / 'source.py').write_text('value = 2\n')
        isolated.view.role_cleanup_pending = {'agent': 'builder', 'phase_id': 'manual',
            'kh_cleanup': {'complete': False, 'surviving_pids': [child.pid], 'root_reaped': False}}
        with self.assertRaises(RuntimeError) as caught:
            with isolated:
                isolated.salvage()
        self.assertTrue(process_io.cleanup_incomplete(caught.exception))
        with self.assertRaises(RuntimeError):
            isolated.promote(core.BuildOutput(status='success', summary='not accepted'))
        temp_reference = weakref.ref(isolated.temp)
        del isolated
        gc.collect()
        self.assertIsNone(temp_reference(), 'the directory survives after TemporaryDirectory itself is collected')
        self.assertTrue(root.is_dir())
        self.assertEqual(process_io.process_identity(child.pid), birth)
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 1\n')

    def test_clean_timeout_keeps_existing_guarded_salvage(self):
        def failed(request, **_):
            root = Path(request.cwd)
            self.worker_roots.add(root)
            (root / 'source.py').write_text('value = 2\n')
            raise process_io.RoleTimeout('clean supervisor timeout', self.partial())
        with self.assertRaises(process_io.RoleTimeout) as caught:
            self.call(failed)
        self.assertTrue(caught.exception.resume_safe)
        self.assertEqual(caught.exception.salvaged_paths, ['source.py'])
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 2\n')
        self.assertFalse(getattr(self.run, 'role_cleanup_pending', None))
        self.assertFalse(next(iter(self.worker_roots)).exists())

    def test_receipt_failure_still_retains_cwd_without_masking_failure(self):
        from adw_modules import scope
        phase = SimpleNamespace(phase_id='receipt-failure', params=SimpleNamespace(owner='builder'))
        isolated = workspace.AgentWorkspace(self.run, phase, self.agent)
        root = isolated.root
        self.worker_roots.add(root)
        child, birth = self.living_child(root)
        error = RuntimeError('callback original')
        error.kh_cleanup = {'complete': False, 'surviving_pids': [child.pid]}
        with patch.object(scope, '_atomic_json', side_effect=OSError('fixture receipt denied')):
            with self.assertRaises(RuntimeError) as caught:
                with isolated:
                    raise error
        self.assertIs(caught.exception, error)
        self.assertIn('fixture receipt denied', self.run.role_cleanup_pending['receipt_error'])
        self.assertEqual(error.retained_workspace, str(root))
        temp_reference = weakref.ref(isolated.temp)
        del isolated
        gc.collect()
        self.assertIsNone(temp_reference())
        self.assertTrue(root.is_dir())
        self.assertEqual(process_io.process_identity(child.pid), birth)

    def test_joined_run_reads_own_pending_receipt_before_dispatch(self):
        def failed(request, **_):
            self.worker_roots.add(Path(request.cwd))
            raise process_io.RoleCleanupError('fixture stop incomplete', self.partial(),
                {'complete': False, 'surviving_pids': [999999]})
        with self.assertRaises(process_io.RoleCleanupError):
            self.call(failed)
        self.run = core.Run(self.cfg, self.run.adw_id, self.tracer, 'joined')
        self.assertFalse(getattr(self.run, 'role_cleanup_pending', None))
        sends = []
        def must_not_send(*args, **kwargs):
            sends.append(args)
            raise AssertionError('restart must not erase unresolved cleanup ownership')
        with self.assertRaises(RuntimeError) as caught:
            self.call(must_not_send, name='joined_must_not_restart')
        self.assertEqual(sends, [])
        self.assertTrue(process_io.cleanup_incomplete(caught.exception))
        self.assertIn('pending_receipt', self.run.role_cleanup_pending)
        self.assertEqual(Path(caught.exception.retained_workspace), next(iter(self.worker_roots)))

    def test_malformed_or_symlinked_pending_receipt_blocks_without_reading_external_file(self):
        folder = Path(self.run.context_handoff_dir) / 'role-cleanup-pending'
        folder.mkdir()
        receipt = folder / 'owner.json'
        outside = Path(self.temp.name) / 'unrelated-owner-file'
        outside.write_text('{"unrelated":"unchanged"}')
        for text in ('not json', '{}', '{"schema_version":999}'):
            with self.subTest(text=text):
                receipt.write_text(text)
                with self.assertRaises(RuntimeError) as caught:
                    workspace.require_role_cleanup(self.run)
                self.assertTrue(process_io.cleanup_incomplete(caught.exception))
                self.assertIn('receipt_error', self.run.role_cleanup_pending)
                del self.run.role_cleanup_pending
        receipt.unlink()
        receipt.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            workspace.require_role_cleanup(self.run)
        self.assertEqual(outside.read_text(), '{"unrelated":"unchanged"}')
        del self.run.role_cleanup_pending
        receipt.unlink()
        (folder / 'unfinished-receipt.tmp').write_text('partial write')
        with self.assertRaises(RuntimeError):
            workspace.require_role_cleanup(self.run)

    def test_real_native_cutoff_failed_signals_retains_cwd_cost_and_unrelated_process(self):
        unrelated, unrelated_birth = self.living_child(self.root)
        script = Path(self.temp.name) / 'fake-agent'
        ready = Path(self.temp.name) / 'provider-ready'
        events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'partial'}],
                 'usage': {'input_tokens': 4, 'output_tokens': 6}}},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
                 'result': 'partial', 'total_cost_usd': 1.25},
        ]
        script.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nimport time\n' +
            "Path('source.py').write_text('value = 2\\n')\n" +
            '\n'.join(f'print({json.dumps(event)!r},flush=True)' for event in events) +
            f'\nPath({str(ready)!r}).write_text("ready")\ntime.sleep(60)\n')
        script.chmod(0o700)
        self.agent.timeout_seconds = 3
        start = process_io.CommandProcessScope.start
        def remember_start(scope, child):
            self.remember(child)
            self.native_child = child
            return start(scope, child)
        native_run = agent_cc.run
        dispatches = []
        def track(request, **callbacks):
            dispatches.append(request)
            self.worker_roots.add(Path(request.cwd))
            try:
                return native_run(request, **callbacks)
            except process_io.RoleCleanupError as error:
                holder = getattr(error, 'retained_policy_directory', None)
                if holder:
                    directory = Path(holder)
                    self.assertTrue(directory.name.startswith('kh-claude-settings-'))
                    info = directory.lstat()
                    self.policy_directories[directory] = (info.st_dev, info.st_ino)
                row = self.tracer.conn.execute('SELECT ended_at FROM processes WHERE pid=?',
                    (self.native_child.pid,)).fetchone()
                self.assertIsNotNone(row)
                self.assertIsNone(row[0], 'a live provider must not receive a process-end event')
                raise
        before_env = dict(os.environ)
        with patch.object(agent_cc, 'CC_PATH', str(script)), \
                patch.object(process_io.CommandProcessScope, 'start', remember_start), \
                patch.object(process_io.CommandProcessScope, '_signal', return_value=None):
            with self.assertRaises(process_io.RoleCleanupError) as caught:
                self.call(track)
            self.assertEqual(len(dispatches), 1)
        self.assertTrue(ready.exists(), 'the actual provider emitted usage and modified its own source')
        self.assertIsNone(self.native_child.poll(), 'signals were denied, so CWD must remain owned')
        self.assertIn(self.native_child.pid, caught.exception.kh_cleanup['surviving_pids'])
        self.assertEqual((self.run.tokens, self.run.cost, self.run.cost_known), (10, 1.25, False))
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 1\n')
        self.assert_retained(caught.exception, next(iter(self.worker_roots)))
        row = self.tracer.conn.execute('SELECT ended_at FROM processes WHERE pid=?',
            (self.native_child.pid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0], 'owner failure reporting must preserve the known-live row')
        self.assertEqual(process_io.process_identity(unrelated.pid), unrelated_birth)
        self.assertIsNone(unrelated.poll())
        self.assertEqual(dict(os.environ), before_env)


class CleanupTransientTests(unittest.TestCase):
    def test_known_incomplete_cleanup_overrides_old_outage_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'raw_output.jsonl').write_text(json.dumps({'type': 'error', 'message': '503 service unavailable'}) + '\n')
            error = RuntimeError('callback failed')
            error.kh_cleanup = {'complete': False, 'surviving_pids': [999999]}
            self.assertEqual(transient.classify(error, path), '')
            error.kh_cleanup['complete'] = True
            self.assertTrue(transient.classify(error, path), 'a clean stop retains normal outage handling')


if __name__ == '__main__':
    unittest.main()
