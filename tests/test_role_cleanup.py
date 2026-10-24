"""Role cancellation releases owned processes, real pipe handles and workers."""
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
ADWS = ROOT / '.claude/skills/kh/templates/adws'
sys.path.insert(0, str(ADWS))
from adw_modules import process_io


FIXTURE = r'''
import json,os,select,signal,socket,array,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,@@ADWS@@)
from adw_modules import process_io
mode,folder=sys.argv[1],Path(sys.argv[2])
def remember(path,pid):
    temporary=path.with_name('.'+path.name+'.tmp')
    temporary.write_text(json.dumps({'pid':pid,'birth':process_io.process_identity(pid)}))
    temporary.replace(path)
remember(folder/'root.pid',os.getpid())
code=("import json,os,sys,time;from pathlib import Path;sys.path.insert(0,"+repr(@@ADWS@@)+");"
      "from adw_modules import process_io;target=Path(sys.argv[1]);"
      "temporary=target.with_name('.'+target.name+'.tmp');"
      "temporary.write_text(json.dumps({'pid':os.getpid(),'birth':process_io.process_identity(os.getpid())}));"
      "temporary.replace(target);time.sleep(60)")
child=subprocess.Popen([sys.executable,'-c',code,str(folder/'child.pid')],start_new_session=True,
    stdin=None if mode=='stdin_holder' else subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
deadline=time.monotonic()+3
while not (folder/'child.pid').exists() and time.monotonic()<deadline:
    time.sleep(.01)
if not (folder/'child.pid').exists(): raise RuntimeError('detached fixture child did not start')
if mode=='stdin_holder':
    if not select.select([0],[],[],3)[0]: raise RuntimeError('stdin writer did not transfer bytes')
    (folder/'stdin-ready').touch()
if mode=='foreign_output':
    connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    connection.connect(sys.argv[3])
    connection.sendmsg([b'x'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[1]))])
    connection.close()
print('READY',flush=True)
print('role-stderr',file=sys.stderr,flush=True)
(folder/'ready').touch()
if mode.startswith('exit_'):
    raise SystemExit(int(mode.split('_',1)[1]))
time.sleep(60)
'''.replace('@@ADWS@@', repr(str(ADWS)))


class RoleCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix='role-fixture-', dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.fixture = self.folder / 'role.py'
        self.fixture.write_text(FIXTURE)
        self.primary, self.foreign, self.workers = [], [], []
        self.identities = {}
        self.addCleanup(self.cleanup_resources)

    def remember_children(self):
        for name in ('root.pid', 'child.pid'):
            path = self.folder / name
            if path.exists():
                record = json.loads(path.read_text())
                if record['birth'] is not None:
                    self.identities[record['pid']] = tuple(record['birth'])

    def kill_fixture_processes(self):
        self.remember_children()
        for pid, identity in list(self.identities.items()):
            if process_io.process_identity(pid) == identity:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def cleanup_resources(self):
        # Kill exact fixture identities before touching a possibly locked
        # TextIO stdin handle. A blocked writer must first get EPIPE/EOF.
        self.kill_fixture_processes()
        for child in [*self.primary, *self.foreign]:
            child.wait(timeout=5)
        for worker in self.workers:
            owner = getattr(getattr(worker, '_target', None), '__self__', None)
            if getattr(owner, 'process', None) in self.primary and callable(getattr(owner, 'close', None)):
                owner.close()
        for worker in self.workers:
            if worker.ident is not None:
                worker.join(timeout=2)
        if not any(worker.is_alive() for worker in self.workers):
            for child in [*self.primary, *self.foreign]:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    @staticmethod
    def running(pid, identity):
        if process_io.process_identity(pid) != identity:
            return False
        status = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                capture_output=True, text=True, timeout=2).stdout.strip()
        return bool(status) and not status.startswith('Z')

    def request(self, mode='closed_child', *, input_text=None, extra=(), argv=None):
        return process_io.StreamRequest(argv or [sys.executable, str(self.fixture), mode, str(self.folder), *extra],
                                       str(self.folder), dict(os.environ), 5, input_text=input_text)

    def drive(self, request, on_line, *, on_spawn=None, on_exit=None,
              start_failure=None, start_failure_at=1):
        popen, thread_class, start_thread = subprocess.Popen, threading.Thread, threading.Thread.start
        finished = threading.Event()
        self.start_attempts, self.started_workers = [], []
        self.watchdog_fired = False
        def rescue():
            if not finished.wait(8):
                self.watchdog_fired = True
                self.kill_fixture_processes()
        watchdog = thread_class(target=rescue, daemon=True)
        watchdog.start()
        def record_process(argv, *args, **kwargs):
            child = popen(argv, *args, **kwargs)
            if list(argv) == request.argv:
                self.primary.append(child)
                identity = process_io.process_identity(child.pid)
                self.assertIsNotNone(identity)
                self.identities[child.pid] = identity
            return child
        def record_worker(*args, **kwargs):
            worker = thread_class(*args, **kwargs)
            self.workers.append(worker)
            return worker
        def start_worker(worker, *args, **kwargs):
            observed = worker in self.workers
            if observed:
                self.start_attempts.append(worker)
                if start_failure is not None and len(self.start_attempts) == start_failure_at:
                    raise start_failure
            result = start_thread(worker, *args, **kwargs)
            if observed:
                self.started_workers.append(worker)
            return result
        started = time.monotonic()
        try:
            with patch.object(process_io.subprocess, 'Popen', side_effect=record_process), \
                 patch.object(process_io.threading, 'Thread', side_effect=record_worker), \
                 patch.object(thread_class, 'start', start_worker):
                return process_io.run_stream(request, on_line, on_spawn=on_spawn, on_exit=on_exit)
        finally:
            finished.set()
            watchdog.join(timeout=1)
            self.remember_children()
            self.assertFalse(self.watchdog_fired, 'role cancellation blocked until the bounded fixture rescue')
            self.assertLess(time.monotonic() - started, 7, 'role cancellation exceeded bounded cleanup grace')

    def assert_stopped_and_closed(self, *, detached=True):
        self.assertEqual(len(self.primary), 1, 'a real primary role process must have started')
        root = self.primary[0]
        self.assertIsNotNone(root.poll(), 'the actual role leader must be reaped before return/raise')
        if detached:
            path = self.folder / 'child.pid'
            self.assertTrue(path.is_file(), 'the detached child must actually start before cancellation')
            record = json.loads(path.read_text())
            self.assertIsNotNone(record['birth'])
            self.assertFalse(self.running(record['pid'], tuple(record['birth'])),
                             'owned detached child survived role cancellation')
        for stream in (root.stdin, root.stdout, root.stderr):
            if stream is not None:
                self.assertTrue(stream.closed, 'every owned Popen pipe must close before return/raise')
        self.assertTrue(self.workers, 'the real supervisor workers must be observed')
        self.assertFalse(any(worker.is_alive() for worker in self.workers),
                         'an actual reader/writer/watcher thread survived role cancellation')

    def wait_ready(self, path, *, process=None):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                break
            time.sleep(.01)
        self.assertTrue(path.exists(), 'the real fixture must report readiness before cancellation')

    def assert_failed_exit_cleanup(self, code):
        lines = []
        request = self.request('exit_' + str(code))
        original_env = dict(request.env)
        result = self.drive(request, lines.append)
        self.assertEqual(result, (code, 'role-stderr\n'))
        self.assertFalse(result.timed_out, 'a literal exit code must not become a supervisor timeout')
        self.assertEqual(lines, ['READY\n'])
        self.assertEqual(request.env, original_env, 'role tracking must not mutate caller environment')
        self.assert_stopped_and_closed()

    def test_on_line_runtime_error_preserves_failure_and_stops_detached_child(self):
        failure = RuntimeError('fixture on_line failure')
        def cancel(line):
            self.assertEqual(line, 'READY\n')
            raise failure
        with self.assertRaises(RuntimeError) as caught:
            self.drive(self.request(), cancel)
        self.assertIs(caught.exception, failure)
        self.assert_stopped_and_closed()

    def test_worker_start_failure_after_real_popen_reaps_root_and_closes_pipes(self):
        failure = RuntimeError('fixture role thread start failure')
        request = self.request(argv=[sys.executable, '-c', 'import time;time.sleep(60)'])
        with self.assertRaises(RuntimeError) as caught:
            self.drive(request, lambda line: None, start_failure=failure)
        self.assertIs(caught.exception, failure)
        self.assert_stopped_and_closed(detached=False)

    def test_reader_start_failure_after_watcher_and_first_reader_started(self):
        failure = RuntimeError('fixture partial reader startup failure')
        request = self.request(argv=[sys.executable, '-c', 'import time;time.sleep(60)'])
        with self.assertRaises(RuntimeError) as caught:
            self.drive(request, lambda line: None, start_failure=failure, start_failure_at=3)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(self.started_workers), 2,
                         'the real watcher and first reader must start before the injected failure')
        self.assert_stopped_and_closed(detached=False)

    def test_stdin_start_failure_after_watcher_and_both_readers_started(self):
        failure = RuntimeError('fixture stdin worker startup failure')
        request = self.request(input_text='unused input',
                               argv=[sys.executable, '-c', 'import time;time.sleep(60)'])
        with self.assertRaises(RuntimeError) as caught:
            self.drive(request, lambda line: None, start_failure=failure, start_failure_at=4)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(self.started_workers), 3,
                         'the real watcher and both readers must start before the stdin start failure')
        self.assert_stopped_and_closed(detached=False)

    def test_on_line_keyboard_interrupt_stops_owned_processes(self):
        failure = KeyboardInterrupt('fixture owner cancellation')
        def cancel(line):
            self.assertEqual(line, 'READY\n')
            raise failure
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.drive(self.request(), cancel)
        self.assertIs(caught.exception, failure)
        self.assert_stopped_and_closed()

    def test_on_spawn_failure_after_detached_child_is_ready(self):
        failure = RuntimeError('fixture on_spawn failure')
        def cancel(pid):
            self.assertEqual(pid, self.primary[0].pid)
            self.wait_ready(self.folder / 'ready', process=self.primary[0])
            record = json.loads((self.folder / 'child.pid').read_text())
            self.assertTrue(self.running(record['pid'], tuple(record['birth'])))
            raise failure
        with self.assertRaises(RuntimeError) as caught:
            self.drive(self.request(), lambda line: None, on_spawn=cancel)
        self.assertIs(caught.exception, failure)
        self.assert_stopped_and_closed()

    def test_on_exit_failure_cleans_child_after_leader_already_exited(self):
        failure = RuntimeError('fixture on_exit failure')
        lines = []
        def cancel(pid):
            self.assertEqual(pid, self.primary[0].pid)
            self.assertEqual(self.primary[0].poll(), 0)
            self.assertTrue((self.folder / 'child.pid').is_file())
            raise failure
        with self.assertRaises(RuntimeError) as caught:
            self.drive(self.request('exit_0'), lines.append, on_exit=cancel)
        self.assertIs(caught.exception, failure)
        self.assertEqual(lines, ['READY\n'])
        self.assert_stopped_and_closed()

    def test_on_exit_failure_does_not_replace_original_callback_failure(self):
        primary = RuntimeError('fixture original callback failure')
        secondary = RuntimeError('fixture secondary exit failure')
        def cancel(line):
            raise primary
        def fail_exit(pid):
            raise secondary
        with self.assertRaises(RuntimeError) as caught:
            self.drive(self.request(), cancel, on_exit=fail_exit)
        self.assertIs(caught.exception, primary)
        self.assert_stopped_and_closed()

    def test_nonzero_exit_stops_detached_child_without_timeout(self):
        self.assert_failed_exit_cleanup(1)

    def test_literal_exit_124_stops_detached_child_without_timeout(self):
        self.assert_failed_exit_cleanup(124)

    def test_zero_exit_keeps_intentional_background_child_and_closes_resources(self):
        lines = []
        result = self.drive(self.request('exit_0'), lines.append)
        self.assertEqual(result, (0, 'role-stderr\n'))
        self.assertFalse(result.timed_out)
        record = json.loads((self.folder / 'child.pid').read_text())
        self.assertTrue(self.running(record['pid'], tuple(record['birth'])),
                        'normal success retains the existing intentional background policy')
        self.assert_stopped_and_closed(detached=False)

    def test_foreign_output_holder_survives_while_cancelled_reader_closes(self):
        # macOS limits AF_UNIX paths. Keep the owned socket under /tmp even
        # when the extracted-release checkout and TMPDIR have longer names.
        socket_temp = tempfile.TemporaryDirectory(prefix='khr-', dir='/tmp')
        self.addCleanup(socket_temp.cleanup)
        socket_path = Path(socket_temp.name) / 'fd.sock'
        receiver_ready = Path(socket_temp.name) / 'ready'
        received = Path(socket_temp.name) / 'received'
        code = r'''
import array,socket,sys,time
from pathlib import Path
listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
listener.bind(sys.argv[1]);listener.listen(1)
Path(sys.argv[2]).touch()
connection,_=listener.accept()
_,ancillary,_,_=connection.recvmsg(1,socket.CMSG_SPACE(array.array('i').itemsize))
held=array.array('i');held.frombytes(ancillary[0][2][:array.array('i').itemsize])
Path(sys.argv[3]).touch()
time.sleep(60)
'''
        env = dict(os.environ)
        env.pop('KH_ROLE_TAG', None)
        env.pop('KH_COMMAND_TAG', None)
        holder = subprocess.Popen([sys.executable, '-c', code, str(socket_path),
                                   str(receiver_ready), str(received)],
                                  env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, start_new_session=True)
        self.foreign.append(holder)
        identity = process_io.process_identity(holder.pid)
        self.assertIsNotNone(identity)
        self.identities[holder.pid] = identity
        self.wait_ready(receiver_ready, process=holder)
        failure = RuntimeError('fixture cancellation with foreign output holder')
        def cancel(line):
            self.assertEqual(line, 'READY\n')
            self.wait_ready(received, process=holder)
            raise failure
        with self.assertRaises(RuntimeError) as caught:
            self.drive(self.request('foreign_output', extra=(str(socket_path),)), cancel)
        self.assertIs(caught.exception, failure)
        self.assertTrue(self.running(holder.pid, identity),
                        'an unrelated process holding the output FD must not be killed')
        self.assert_stopped_and_closed()

    def test_cancelled_large_stdin_writer_stops_without_waiting_for_reader(self):
        failure = RuntimeError('fixture cancellation with blocked stdin')
        def cancel(line):
            self.assertEqual(line, 'READY\n')
            self.assertTrue((self.folder / 'stdin-ready').exists(),
                            'the real stdin writer must transfer bytes before cancellation')
            self.assertIsNotNone(self.primary[0].stdin)
            self.assertFalse(self.primary[0].stdin.closed)
            raise failure
        with self.assertRaises(RuntimeError) as caught:
            self.drive(self.request('stdin_holder', input_text='x' * (8 * 1024 * 1024)), cancel)
        self.assertIs(caught.exception, failure)
        self.assert_stopped_and_closed()

    def test_normal_output_keeps_utf8_newlines_and_final_partial_line(self):
        code = ("import os,time;os.write(1,b'\\xd8');time.sleep(.03);"
                "os.write(1,b'\\xb3\\r\\nlast-line');os.write(2,b'error\\r\\n')")
        lines = []
        result = self.drive(self.request(argv=[sys.executable, '-c', code]), lines.append)
        self.assertEqual(lines, ['س\n', 'last-line'])
        self.assertEqual(result, (0, 'error\n'))
        self.assertFalse(result.timed_out)
        self.assert_stopped_and_closed(detached=False)

    def test_stdin_keeps_text_wrapper_replacement_for_surrogate_input(self):
        code = 'import sys;print(sys.stdin.buffer.read().hex(),flush=True)'
        lines = []
        result = self.drive(self.request(argv=[sys.executable, '-c', code],
                                         input_text='\udcff'), lines.append)
        self.assertEqual(lines, ['3f\n'])
        self.assertEqual(result, (0, ''))
        self.assertFalse(result.timed_out)
        self.assert_stopped_and_closed(detached=False)

    def test_lifecycle_callbacks_run_once_on_caller_thread_after_success(self):
        caller = threading.get_ident()
        calls = []
        def spawn(pid):
            calls.append(('spawn', threading.get_ident(), pid))
        def line(value):
            calls.append(('line', threading.get_ident(), value))
        def exited(pid):
            calls.append(('exit', threading.get_ident(), pid))
        request = self.request(argv=[sys.executable, '-c', "print('READY',flush=True)"])
        result = self.drive(request, line, on_spawn=spawn, on_exit=exited)
        pid = self.primary[0].pid
        self.assertEqual(calls, [('spawn', caller, pid), ('line', caller, 'READY\n'),
                                 ('exit', caller, pid)])
        self.assertEqual(result, (0, ''))
        self.assert_stopped_and_closed(detached=False)

    def test_lifecycle_callbacks_run_once_on_caller_thread_after_failure(self):
        caller = threading.get_ident()
        calls = []
        failure = RuntimeError('fixture lifecycle failure')
        def spawn(pid):
            calls.append(('spawn', threading.get_ident(), pid))
        def line(value):
            calls.append(('line', threading.get_ident(), value))
            raise failure
        def exited(pid):
            calls.append(('exit', threading.get_ident(), pid))
        code = "import time;print('READY',flush=True);time.sleep(60)"
        with self.assertRaises(RuntimeError) as caught:
            self.drive(self.request(argv=[sys.executable, '-c', code]), line,
                       on_spawn=spawn, on_exit=exited)
        self.assertIs(caught.exception, failure)
        pid = self.primary[0].pid
        self.assertEqual(calls, [('spawn', caller, pid), ('line', caller, 'READY\n'),
                                 ('exit', caller, pid)])
        self.assert_stopped_and_closed(detached=False)

    def test_launch_error_does_not_run_lifecycle_callbacks(self):
        calls = []
        request = self.request(argv=[str(self.folder / 'missing-role-executable')])
        with self.assertRaises(FileNotFoundError) as caught:
            self.drive(request, lambda value: calls.append(('line', value)),
                       on_spawn=lambda pid: calls.append(('spawn', pid)),
                       on_exit=lambda pid: calls.append(('exit', pid)))
        self.assertEqual(caught.exception.filename, request.argv[0])
        self.assertEqual(calls, [])
        self.assertEqual(self.primary, [])
        self.assertEqual(self.workers, [])

    def test_queue_allocation_failure_precedes_real_popen_and_callbacks(self):
        failure = RuntimeError('fixture queue allocation failure')
        calls = []
        request = self.request(argv=[sys.executable, '-c', 'import time;time.sleep(60)'])
        with patch.object(process_io.queue, 'Queue', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                self.drive(request, lambda line: calls.append(('line', line)),
                           on_spawn=lambda pid: calls.append(('spawn', pid)),
                           on_exit=lambda pid: calls.append(('exit', pid)))
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.primary, [], 'queue allocation must fail before a real process is spawned')
        self.assertEqual(self.workers, [])
        self.assertEqual(calls, [])

    def test_transient_cleanup_error_preserves_callback_failure_and_retries(self):
        failure = RuntimeError('fixture cancellation before transient cleanup failure')
        transient = OSError('fixture first cleanup attempt failed')
        stop = process_io.CommandProcessScope.stop
        attempts = []
        def retry(scope, *args, **kwargs):
            attempts.append(scope)
            if len(attempts) == 1:
                raise transient
            return stop(scope, *args, **kwargs)
        def cancel(line):
            self.assertEqual(line, 'READY\n')
            raise failure
        with patch.object(process_io.CommandProcessScope, 'stop', retry):
            with self.assertRaises(RuntimeError) as caught:
                self.drive(self.request(), cancel)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(attempts), 2, 'the second cleanup attempt must invoke the real scope stop')
        self.assertIs(attempts[0], attempts[1])
        self.assert_stopped_and_closed()

    def test_transient_cleanup_error_preserves_actual_supervisor_timeout(self):
        transient = OSError('fixture first timeout cleanup attempt failed')
        stop = process_io.CommandProcessScope.stop
        attempts = []
        def retry(scope, *args, **kwargs):
            attempts.append(scope)
            if len(attempts) == 1:
                raise transient
            return stop(scope, *args, **kwargs)
        request = self.request()
        request.timeout = 1
        lines = []
        with patch.object(process_io.CommandProcessScope, 'stop', retry):
            result = self.drive(request, lines.append)
        self.assertEqual(result[0], 124)
        self.assertTrue(result.timed_out, 'the real supervisor cutoff must retain its timeout fact')
        self.assertEqual(lines, ['READY\n'], 'the real detached fixture must be ready before cutoff')
        self.assertEqual(len(attempts), 2, 'the second cleanup attempt must invoke the real scope stop')
        self.assertIs(attempts[0], attempts[1])
        self.assert_stopped_and_closed()


if __name__ == '__main__':
    unittest.main()
