"""Public command deadlines clean only the harmless processes they started."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADWS = ROOT / ".claude/skills/kh/templates/adws"
sys.path.insert(0, str(ADWS))
from adw_modules import process_io
import kh_exec

FIXTURE = r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,@@ADWS@@)
from adw_modules import process_io
mode,folder=sys.argv[1],Path(sys.argv[2])
def save_pid(path,pid):
    temporary=path.with_name('.'+path.name+'.tmp')
    temporary.write_text(json.dumps({'pid':pid,'birth':process_io.process_identity(pid)}))
    temporary.replace(path)
if mode == 'ignore_term':
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
save_pid(folder/'program.pid',os.getpid())
if mode == 'native_reparented':
    middle = subprocess.Popen([sys.executable,'-c',
        "import json,os,subprocess,sys,time;from pathlib import Path;"
        "sys.path.insert(0,"+repr(@@ADWS@@)+");from adw_modules import process_io;"
        "p=subprocess.Popen(['/bin/sleep','60'],env={},stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);"
        "target=Path(sys.argv[1]);temporary=target.with_name('.'+target.name+'.tmp');"
        "temporary.write_text(json.dumps({'pid':p.pid,'birth':process_io.process_identity(p.pid)}));"
        "temporary.replace(target);time.sleep(.8)",
        str(folder/'child.pid')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,start_new_session=True)
    middle.wait()
    print('INTERMEDIATE_EXITED',flush=True)
else:
    code = ("import json,os,signal,sys,time;from pathlib import Path;"
            + "sys.path.insert(0,"+repr(@@ADWS@@)+");from adw_modules import process_io;"
            + ("signal.signal(signal.SIGTERM,signal.SIG_IGN);" if mode=='ignore_term' else '')
            + "target=Path(sys.argv[1]);temporary=target.with_name('.'+target.name+'.tmp');"
            + "temporary.write_text(json.dumps({'pid':os.getpid(),'birth':process_io.process_identity(os.getpid())}));"
            + "temporary.replace(target);"
            + ("print('DETACHED_CHILD_STDOUT',flush=True);print('DETACHED_CHILD_STDERR',file=sys.stderr,flush=True);"
               if mode=='pipe_holder' else '') + "time.sleep(60)")
    streams = {} if mode=='pipe_holder' else {'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
    subprocess.Popen([sys.executable,'-c',code,str(folder/'child.pid')],
                     stdin=subprocess.DEVNULL,start_new_session=True,**streams)
    deadline=time.monotonic()+2
    while not (folder/'child.pid').exists() and time.monotonic()<deadline:
        time.sleep(.01)
    if not (folder/'child.pid').exists():
        raise RuntimeError('fixture child did not start')
print('COMMAND_FIXTURE_STARTED',flush=True)
print('COMMAND_FIXTURE_STDERR',file=sys.stderr,flush=True)
save_pid(folder/'ready.pid',os.getpid())
time.sleep(60)
'''
FIXTURE = FIXTURE.replace('@@ADWS@@', repr(str(ADWS)))


class CommandCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="owned-processes-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.fixture = self.folder / "fixture.py"
        self.fixture.write_text(FIXTURE)
        self.owned = {}
        self.addCleanup(self.cleanup_owned)

    def remember_fixture_pids(self):
        for name in ("program.pid", "child.pid"):
            path = self.folder / name
            if path.exists():
                record = json.loads(path.read_text())
                pid, birth = record["pid"], record["birth"]
                if birth is not None:
                    self.owned[pid] = tuple(birth)

    def cleanup_owned(self):
        self.remember_fixture_pids()
        for pid, identity in self.owned.items():
            if identity == process_io.process_identity(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @staticmethod
    def running(pid):
        if process_io.process_identity(pid) is None:
            return False
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True,
                                text=True, timeout=2)
        status = result.stdout.strip()
        return bool(status) and not status.startswith("Z")

    def argv(self, mode, *, timeout=.45, standalone=False):
        helper = ADWS / "kh_exec.py"
        if standalone:
            helper = self.folder / "standalone/kh_exec.py"
            helper.parent.mkdir()
            shutil.copy2(ADWS / "kh_exec.py", helper)
        interpreter = [sys.executable, "-I", "-S"] if standalone else [sys.executable]
        return [*interpreter, str(helper), "--class", "test", "--timeout", str(timeout),
                "--out", str(self.folder / "receipts"), "--", sys.executable,
                str(self.fixture), mode, str(self.folder)]

    def verify_receipt(self, output, error, expected_code):
        lines = [line for line in output.splitlines() if line.startswith("KH_EXEC_RECEIPT ")]
        self.assertTrue(lines, f"No authentic command receipt:\n{output}\n{error}")
        marker = json.loads(lines[-1].partition(" ")[2])
        directory = Path(marker["directory"])
        self.assertTrue(directory.is_relative_to(self.folder))
        record = json.loads((directory / "receipt.json").read_text())
        self.assertEqual(record["outcome"]["exit_code"], expected_code)
        self.assertEqual(marker["outcome"], record["outcome"])
        self.assertTrue(record["outcome"]["streams_complete"], "cleanup must let captured pipes drain")
        for channel in ("stdout", "stderr"):
            data = (directory / channel).read_bytes()
            self.assertEqual(record[channel]["bytes"], len(data))
            self.assertEqual(record[channel]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(marker[channel], record[channel])
        self.assertIn(b"COMMAND_FIXTURE_STARTED", (directory / "stdout").read_bytes())
        self.assertIn(b"COMMAND_FIXTURE_STDERR", (directory / "stderr").read_bytes())
        return record

    def assert_fixture_stopped(self):
        path = self.folder / "child.pid"
        self.assertTrue(path.is_file(), "the detached fixture child must actually start")
        child_record = json.loads(path.read_text())
        child = child_record["pid"]
        self.assertGreater(child, 1)
        self.assertIsNotNone(child_record["birth"], "the actual child's kernel birth must be known")
        self.assertFalse(tuple(child_record["birth"]) == process_io.process_identity(child) and self.running(child),
                         f"owned detached child {child} survived command return")
        program_record = json.loads((self.folder / "program.pid").read_text())
        program = program_record["pid"]
        self.assertFalse(tuple(program_record["birth"]) == process_io.process_identity(program) and self.running(program),
                         f"owned main program {program} survived command return")

    def invoke(self, mode, *, timeout=.45, standalone=False):
        started = time.monotonic()
        result = subprocess.run(self.argv(mode, timeout=timeout, standalone=standalone), cwd=self.folder,
                                capture_output=True, text=True, timeout=12)
        self.remember_fixture_pids()
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertLess(time.monotonic() - started, timeout + 4, "command cleanup exceeded its bounded grace")
        record = self.verify_receipt(result.stdout, result.stderr, 124)
        self.assertEqual(record["outcome"]["kind"], "timeout")
        self.assert_fixture_stopped()
        return record

    def test_timeout_stops_detached_child_after_it_closes_both_streams(self):
        self.invoke("closed_streams")

    def test_timeout_stops_detached_pipe_holder_and_finishes_authentic_streams(self):
        record = self.invoke("pipe_holder")
        self.assertIn(b"DETACHED_CHILD_STDOUT", (Path(record["directory"]) / "stdout").read_bytes())
        self.assertIn(b"DETACHED_CHILD_STDERR", (Path(record["directory"]) / "stderr").read_bytes())

    def test_timeout_stops_observed_reparented_native_child_with_no_environment(self):
        record = self.invoke("native_reparented", timeout=1.5)
        self.assertIn(b"INTERMEDIATE_EXITED", (Path(record["directory"]) / "stdout").read_bytes())

    def test_unrelated_sentinel_remains_alive_during_owned_cleanup(self):
        sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        def cleanup_sentinel():
            if sentinel.poll() is None:
                sentinel.kill()
            sentinel.wait(timeout=5)
        self.addCleanup(cleanup_sentinel)
        self.invoke("closed_streams")
        self.assertIsNone(sentinel.poll(), "cleanup must never signal unrelated processes")

    def test_inner_command_timeout_is_a_role_failure_with_no_surviving_command_child(self):
        lines = []
        result = process_io.run_stream(process_io.StreamRequest(self.argv("closed_streams"),
            str(self.folder), dict(os.environ), 12), lines.append)
        self.remember_fixture_pids()
        self.assertEqual(result[0], 124)
        self.assertFalse(result.timed_out, "the inner command deadline must not be called a role cutoff")
        self.verify_receipt("".join(lines), result[1], 124)
        self.assert_fixture_stopped()

    def test_sigterm_without_a_timeout_escalates_when_fixture_ignores_term(self):
        helper = subprocess.Popen(self.argv("ignore_term", timeout=0), cwd=self.folder,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        def cleanup_helper():
            if helper.poll() is None:
                helper.kill()
            helper.wait(timeout=5)
            for stream in (helper.stdout, helper.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.addCleanup(cleanup_helper)
        ready = time.monotonic() + 3
        while not (self.folder / "ready.pid").exists() and helper.poll() is None and time.monotonic() < ready:
            time.sleep(.01)
        self.remember_fixture_pids()
        self.assertTrue((self.folder / "ready.pid").is_file(), "captured startup output must precede SIGTERM")
        self.assertTrue((self.folder / "child.pid").is_file(), "TERM-ignoring fixture must be running")
        started = time.monotonic()
        helper.send_signal(signal.SIGTERM)
        try:
            output, error = helper.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("kh_exec did not bound cleanup after SIGTERM with --timeout 0")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(helper.returncode, 128 + signal.SIGTERM, output + error)
        record = self.verify_receipt(output, error, 128 + signal.SIGTERM)
        self.assertEqual(record["outcome"]["kind"], "interrupted")
        self.assert_fixture_stopped()

    def test_copied_standalone_helper_keeps_owned_cleanup_without_repository_imports(self):
        self.invoke("closed_streams", standalone=True)

    def test_pipe_reader_cancellation_closes_reader_without_waiting_for_writer_eof(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, 'rb', buffering=0)
        writer = os.fdopen(write_fd, 'wb', buffering=0)
        target = self.folder / 'cancelled-capture.bin'
        digest, count, errors = hashlib.sha256(), [0], []
        cancel = threading.Event()
        pump = threading.Thread(target=kh_exec._pump,
            args=(reader, target, digest, count, cancel, errors), daemon=True)
        payload = b'captured before cancellation\x00\n'
        pump.start()
        try:
            writer.write(payload)
            deadline = time.monotonic() + 2
            while count[0] < len(payload) and pump.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(count[0], len(payload), 'the live pipe must actually deliver bytes')
            self.assertFalse(writer.closed, 'the writer must still hold EOF open at cancellation')
            cancel.set()
            pump.join(timeout=1)
            self.assertFalse(pump.is_alive(), 'reader cancellation must not wait for writer EOF')
            self.assertTrue(reader.closed, 'the pump owns and must close its read handle')
            self.assertFalse(writer.closed, 'cancelling the reader must not close an unrelated writer')
            self.assertEqual(errors, [])
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual((count[0], digest.hexdigest()), (len(payload), hashlib.sha256(payload).hexdigest()))
            captured = (target.read_bytes(), count[0], digest.hexdigest())
            time.sleep(.06)
            self.assertEqual((target.read_bytes(), count[0], digest.hexdigest()), captured,
                             'no reader may keep mutating retained bytes after cancellation completes')
        finally:
            cancel.set()
            writer.close()
            pump.join(timeout=2)
            if not reader.closed:
                reader.close()

    def test_incomplete_cleanup_retries_and_reaps_the_owned_process(self):
        scope = kh_exec.CommandProcessScope(dict(os.environ))
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
            env=scope.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        identity = process_io.process_identity(child.pid)
        self.assertIsNotNone(identity)
        self.owned[child.pid] = identity
        def cleanup_child():
            if child.poll() is None and process_io.process_identity(child.pid) == identity:
                child.kill()
            child.wait(timeout=5)
        self.addCleanup(cleanup_child)
        self.addCleanup(scope.close)
        scope.start(child)
        self.assertIsNone(child.poll(), 'the owned process must be running before the first stop')
        with patch.object(scope, '_signal', return_value=None) as suppressed:
            first = scope.stop(grace_seconds=.001, reap_seconds=.001)
        self.assertTrue(suppressed.called)
        self.assertFalse(first['complete'])
        self.assertIn(child.pid, first['surviving_pids'])
        self.assertIsNone(child.poll(), 'the first suppressed cleanup must genuinely leave the process alive')
        second = scope.stop(grace_seconds=.02, reap_seconds=1)
        self.assertTrue(second['complete'], 'an incomplete cleanup result must not prevent a real retry')
        self.assertEqual(second['surviving_pids'], [])
        self.assertIsNotNone(child.poll(), 'retry must reap the actual owned child')
        self.assertFalse(process_io.process_identity(child.pid) == identity and self.running(child.pid))

    def test_reused_root_pid_cannot_adopt_an_unrelated_process_tree(self):
        root_pid, unrelated_child = 810001, 810002
        identities = {root_pid: (200,), unrelated_child: (300,)}
        scope = kh_exec.CommandProcessScope({})
        self.addCleanup(scope.close)
        scope.process, scope.identity = SimpleNamespace(pid=root_pid), (100,)
        with patch.object(kh_exec, '_parent_listing', return_value={unrelated_child: root_pid}), \
             patch.object(kh_exec, 'process_identity', side_effect=identities.get), \
             patch.object(kh_exec, '_process_info', return_value=(root_pid, identities[unrelated_child])):
            scope.observe()
        self.assertEqual(scope._known, {}, 'a reused leader PID gives no ownership of its new children')

    def test_watcher_start_failure_reaps_real_child_and_preserves_original_error(self):
        spawned = []
        popen, start_thread = subprocess.Popen, threading.Thread.start
        command = [sys.executable, '-c', 'import time;time.sleep(60)']
        def remember_child(argv, *args, **kwargs):
            child = popen(argv, *args, **kwargs)
            if list(argv) == command:
                spawned.append(child)
                identity = process_io.process_identity(child.pid)
                self.assertIsNotNone(identity)
                self.owned[child.pid] = identity
            return child
        def fail_only_watcher(thread, *args, **kwargs):
            target = getattr(thread, '_target', None)
            if getattr(target, '__func__', None) is kh_exec.CommandProcessScope._watch:
                raise RuntimeError('fixture watcher start failed')
            return start_thread(thread, *args, **kwargs)
        def cleanup_child_handles():
            for child in spawned:
                identity = self.owned[child.pid]
                if child.poll() is None and process_io.process_identity(child.pid) == identity:
                    child.kill()
                child.wait(timeout=5)
                for stream in (child.stdout, child.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
        self.addCleanup(cleanup_child_handles)
        with patch.object(kh_exec.subprocess, 'Popen', side_effect=remember_child), \
             patch.object(kh_exec.threading.Thread, 'start', fail_only_watcher):
            with self.assertRaisesRegex(RuntimeError, '^fixture watcher start failed$'):
                kh_exec.main(['--timeout', '5', '--out', str(self.folder / 'watcher-failure'), '--', *command])
        self.assertEqual(len(spawned), 1, 'fault must occur after a real command child was started')
        child = spawned[0]
        self.assertIsNotNone(child.poll(), 'watcher setup failure must reap the actual command child')
        self.assertTrue(child.stdout.closed)
        self.assertTrue(child.stderr.closed)


if __name__ == "__main__":
    unittest.main()
