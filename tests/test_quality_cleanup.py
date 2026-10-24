"""Real host checks stop only their own children and retain failure evidence."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import quality
from adw_modules.data_types import ConfigDefaults, KHConfig, ObservabilityConfig, PhaseParams, QualityCheckSpec
from adw_modules.runner import Run
from adw_modules.tracer import Tracer
from kh_exec import process_identity, process_running


class QualityCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-quality-cleanup-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.work = self.base / 'project'; self.work.mkdir()
        (self.work / 'source.py').write_text('value = 7\n')
        previous = Path.cwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, previous)
        self.cfg = KHConfig(defaults=ConfigDefaults(data_dir='adws/adw_data'),
                            observability=ObservabilityConfig(db='adws/adw_data/kh.db'))
        self.tracer = Tracer(self.cfg.observability.db, 'adws/adw_data/sessions/check/events.jsonl')
        self.addCleanup(self.tracer.conn.close)
        self.run = Run(self.cfg, 'check', self.tracer, 'host-quality-cleanup')
        self.tracer.session_start('check', 'host-quality-cleanup')
        self.pids = self.base / 'pids.json'
        self.addCleanup(self.stop_fixture_children)

    def stop_fixture_children(self):
        if self.pids.exists():
            for row in json.loads(self.pids.read_text()):
                pid, identity = row['pid'], tuple(row['identity']) if row['identity'] else None
                if identity is None or process_identity(pid) != identity:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def script(self, *, leader_exits=False, ignores_term=False):
        child = ('import signal,time; '
                 + ('signal.signal(signal.SIGTERM,signal.SIG_IGN); ' if ignores_term else '')
                 + 'print("child-ready",flush=True); time.sleep(30)')
        return ('import json,os,subprocess,sys,time; '
                f'sys.path.insert(0,{str(ROOT / ".claude/skills/kh/templates/adws")!r}); '
                'from kh_exec import process_identity; '
                f'child=subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
                f'open({str(self.pids)!r},"w").write(json.dumps([{{"pid":child.pid,"identity":process_identity(child.pid)}}])); '
                'print("stdout-before-timeout",flush=True); '
                'print("stderr-before-timeout",file=sys.stderr,flush=True); '
                'time.sleep(0.2); '
                + ('sys.exit(0)' if leader_exits else 'time.sleep(30)'))

    def execute(self, code, timeout=1):
        with self.run.phase(PhaseParams(name='quality', kind='code', owner='quality',
                                       description='Capture actual subprocess timeout and descendant cleanup')):
            return quality._run(QualityCheckSpec(name='cleanup', area='backend', operation='build',
                argv=[sys.executable, '-c', code], timeout_seconds=timeout), self.run)

    def assert_children_stopped(self):
        logs = list(Path(self.run.context_handoff_dir).rglob('command.log'))
        self.assertEqual(len(logs), 1)
        stdout = logs[0].read_text().split('--- stdout ---\n', 1)[1].split('\n--- stderr ---', 1)[0]
        self.assertIn('child-ready', stdout.splitlines(), 'child must reach its ready point before cleanup is judged')
        owners = json.loads(self.pids.read_text())
        self.assertTrue(owners)
        for row in owners:
            self.assertIsNotNone(row['identity'], 'fixture must retain a kernel birth identity')
        for _ in range(25):
            if all(not process_running(row['pid'], tuple(row['identity'])) for row in owners):
                return
            time.sleep(0.02)
        self.fail('a quality command left its recorded detached child running')

    def assert_timeout_evidence(self, result):
        self.assertEqual(result.returncode, 124)
        self.assertFalse(result.passed)
        self.assertLess(result.duration_seconds, 4.5)
        log = Path(result.output_artifact).read_text()
        self.assertIn('exit: 124\n', log)
        self.assertIn('stdout-before-timeout', log.split('--- stdout ---', 1)[1])
        self.assertIn('stderr-before-timeout', log.split('--- stderr ---', 1)[1])
        self.assertIn('Timed out after', result.output_tail)
        before = log.split('source_before: ', 1)[1].splitlines()[0]
        after = log.split('source_after: ', 1)[1].splitlines()[0]
        self.assertEqual(before, after)
        self.assertEqual((self.work / 'source.py').read_text(), 'value = 7\n')

    def test_detached_pipe_holder_stops_with_authentic_timeout_and_partial_log(self):
        result = self.execute(self.script())
        self.assert_timeout_evidence(result)
        self.assert_children_stopped()

    def test_pipe_holder_is_stopped_after_its_leader_already_exited(self):
        result = self.execute(self.script(leader_exits=True))
        self.assert_timeout_evidence(result)
        self.assert_children_stopped()

    def test_term_ignoring_detached_child_is_killed(self):
        result = self.execute(self.script(ignores_term=True))
        self.assert_timeout_evidence(result)
        self.assertIn('child-ready', Path(result.output_artifact).read_text().split('--- stdout ---', 1)[1])
        self.assert_children_stopped()

    def test_unrelated_same_role_process_survives_and_parent_environment_is_unchanged(self):
        with patch.dict(os.environ, {'KH_ROLE_TAG': 'shared-role-fixture', 'KH_QUALITY_OWNER_VALUE': 'unchanged'}):
            before = dict(os.environ)
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                         start_new_session=True, env=dict(os.environ))
            try:
                result = self.execute(self.script())
                self.assert_timeout_evidence(result)
                self.assert_children_stopped()
                self.assertIsNone(unrelated.poll(), 'per-command cleanup must not stop another command in the role')
                self.assertEqual(dict(os.environ), before)
            finally:
                unrelated.kill(); unrelated.wait(timeout=2)

    def test_caller_interrupt_after_leader_exit_stops_child_and_preserves_command_log(self):
        interrupt = threading.Timer(0.6, lambda: os.kill(os.getpid(), signal.SIGINT))
        interrupt.daemon = True
        interrupt.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.execute(self.script(leader_exits=True), timeout=10)
        finally:
            interrupt.cancel()
        self.assert_children_stopped()
        logs = list(Path(self.run.context_handoff_dir).rglob('command.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('stdout-before-timeout', logs[0].read_text().split('--- stdout ---', 1)[1])

    def test_normal_check_preserves_environment_and_source_fingerprint(self):
        before = dict(os.environ)
        result = self.execute('import os; print("role="+os.environ.get("KH_ROLE_TAG","none")); '
                              'print("command-tag="+os.environ.get("KH_COMMAND_TAG","missing"))')
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.passed)
        self.assertNotIn('command-tag=missing', result.output_tail)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual((self.work / 'source.py').read_text(), 'value = 7\n')

    def test_source_mutating_check_still_fails_the_existing_verification_guard(self):
        result = self.execute('from pathlib import Path; Path("source.py").write_text("value = 99\\n"); '
                              'print("process succeeded")')
        self.assertEqual(result.returncode, 125)
        self.assertFalse(result.passed)
        self.assertIn('VERIFICATION_SOURCE_CHANGED', result.output_tail)

    def test_foreign_pipe_holder_preserves_partial_log_without_killing_unrelated_process(self):
        # Transfer only stdout to an already-running fixture via local IPC. It
        # is not a command descendant and must not be killed by command scope.
        # macOS limits AF_UNIX address bytes even when the filesystem accepts
        # the release verifier's much longer TMPDIR. Only the socket is short.
        socket_temp = tempfile.TemporaryDirectory(prefix='khq-', dir=str(Path('/tmp').resolve()))
        self.addCleanup(socket_temp.cleanup)
        socket_path, ready = Path(socket_temp.name) / 'pipe.sock', self.base / 'pipe-ready'
        receiver = ('import array,socket,time; from pathlib import Path; '
                    'listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); '
                    f'listener.bind({str(socket_path)!r}); listener.listen(1); '
                    f'Path({str(ready)!r}).touch(); connection,_=listener.accept(); '
                    'message,ancillary,flags,address=connection.recvmsg(1,socket.CMSG_SPACE(4)); '
                    'held=array.array("i"); held.frombytes(ancillary[0][2][:4]); time.sleep(30)')
        holder = subprocess.Popen([sys.executable, '-c', receiver], start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 3
            while not ready.exists() and holder.poll() is None and time.monotonic() < until:
                time.sleep(0.01)
            if not ready.exists():
                if holder.poll() is None:
                    holder.kill()
                _, receiver_stderr = holder.communicate(timeout=2)
                self.fail(f'local fixture did not become ready: exit={holder.returncode}, '
                          f'socket_bytes={len(os.fsencode(socket_path))}, stderr={receiver_stderr!r}')
            sender = ('import array,socket,sys; connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); '
                      f'connection.connect({str(socket_path)!r}); '
                      'connection.sendmsg([b"x"],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array("i",[1]))]); '
                      'print("stdout-before-timeout",flush=True); '
                      'print("stderr-before-timeout",file=sys.stderr,flush=True)')
            actual_popen, owned_commands = subprocess.Popen, []
            def record_owned_command(*args, **kwargs):
                process = actual_popen(*args, **kwargs)
                if args and args[0] == [sys.executable, '-c', sender]:
                    owned_commands.append(process)
                return process
            with patch.object(quality.subprocess, 'Popen', side_effect=record_owned_command):
                result = self.execute(sender)
            self.assert_timeout_evidence(result)
            self.assertIn('COMMAND_STREAMS_INCOMPLETE', result.output_tail)
            self.assertIn('streams_complete: false', Path(result.output_artifact).read_text())
            self.assertIsNone(holder.poll(), 'an unrelated pipe holder must survive scope cleanup')
            self.assertEqual(len(owned_commands), 1)
            self.assertTrue(owned_commands[0].stdout.closed, 'owned stdout must close after bounded capture')
            self.assertTrue(owned_commands[0].stderr.closed, 'owned stderr must close after bounded capture')
        finally:
            if holder.poll() is None:
                holder.kill()
            try:
                holder.communicate(timeout=2)
            finally:
                holder.stderr.close()
            self.assertTrue(holder.stderr.closed, 'fixture receiver pipe must close at scoped teardown')


if __name__ == '__main__':
    unittest.main()
