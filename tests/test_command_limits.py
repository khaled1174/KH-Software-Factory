"""Builder command limits and timeout cleanup of identifiable role descendants."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests import test_start as startup

sys.path.insert(0, str(startup.ADWS))
from adw_modules import command_limits, process_io
from adw_modules.process_io import StreamRequest, run_stream


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie has stopped; ps also handles this check on macOS.
    state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                           capture_output=True, text=True, timeout=2).stdout.strip()
    return bool(state) and not state.startswith("Z")


def stop_fixture_children(pidfile):
    if pidfile.exists():
        for pid in (int(value) for value in pidfile.read_text().split()):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class CommandLimitTests(unittest.TestCase):
    def test_limit_is_a_quarter_of_the_role_between_floor_and_cap(self):
        for role, expected in ((2400, 600), (3000, 600), (7200, 600), (1200, 300), (400, 100), (60, 30), (1, 30)):
            with self.subTest(role=role):
                self.assertEqual(command_limits.command_limit(role), expected)

    def test_unreadable_role_time_falls_back_to_the_cap(self):
        for bad in (None, "x", float("inf")):
            with self.subTest(bad=bad):
                self.assertEqual(command_limits.command_limit(bad), command_limits.COMMAND_CAP_SECONDS)

    def test_instruction_states_both_numbers(self):
        text = command_limits.instruction(2400)
        self.assertIn("2400 seconds in total", text)
        self.assertIn("at most 600 seconds", text)
        self.assertIn("timeout 600 <command>", text)


class InstructionReachesOnlyTheBuilderTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke

    def test_builder_prompt_carries_the_limit_and_reviewer_prompt_does_not(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = self.root / 'adws/adw_data/sessions/guarded'
        builder = next(run.glob('builder/phases/*/attempt-001/user.md')).read_text()
        reviewer = next(run.glob('reviewer/phases/*/attempt-001/user.md')).read_text()
        self.assertIn("Controller command time limit", builder)
        self.assertNotIn("Controller command time limit", reviewer)


class OrphanSweepTests(unittest.TestCase):
    def test_observed_native_descendant_is_stopped_after_reparenting(self):
        folder = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(folder.cleanup)
        pidfile = Path(folder.name) / 'native.pid'
        release = Path(folder.name) / 'release'
        self.addCleanup(stop_fixture_children, pidfile)
        sleep = shutil.which('sleep')
        self.assertIsNotNone(sleep, 'the POSIX sleep executable is required')
        intermediate = textwrap.dedent(f"""
            import subprocess, time
            from pathlib import Path
            child = subprocess.Popen([{sleep!r}, '60'], start_new_session=True)
            Path({str(pidfile)!r}).write_text(str(child.pid))
            print('native ready', flush=True)
            while not Path({str(release)!r}).exists():
                time.sleep(0.01)
        """)
        role = textwrap.dedent(f"""
            import subprocess, sys, time
            from pathlib import Path
            subprocess.run([sys.executable, '-c', {intermediate!r}], check=True)
            pid = Path({str(pidfile)!r}).read_text()
            parent = subprocess.run(['ps', '-p', pid, '-o', 'ppid='],
                                    capture_output=True, text=True, check=True).stdout.strip()
            print('reparented to ' + parent, flush=True)
            time.sleep(60)
        """)
        lines = []

        def ready(line):
            lines.append(line)
            if line == 'native ready\n':
                # The supervisor can observe the live chain before this
                # callback lets the intermediate parent exit.
                release.touch()

        rc, err = run_stream(StreamRequest([sys.executable, '-c', role], os.getcwd(), dict(os.environ), 2), ready)
        self.assertEqual(rc, 124)
        self.assertEqual(lines, ['native ready\n', 'reparented to 1\n'])
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(alive(pid), 'the observed native descendant survived after its parent exited')
        self.assertIn('Stopped 1 process(es)', err)

    def test_process_that_left_the_group_and_the_parent_chain_is_stopped(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        pidfile = Path(folder.name) / "pids"
        self.addCleanup(stop_fixture_children, pidfile)
        detached = textwrap.dedent(f"""
            import os, time
            from pathlib import Path
            Path({str(pidfile)!r}).write_text(str(os.getpid()))
            while os.getppid() != 1:
                time.sleep(0.01)
            print('reparented to 1', flush=True)
            time.sleep(60)
        """)
        intermediate = ("import subprocess,sys; "
                        f"subprocess.Popen([sys.executable, '-c', {detached!r}], start_new_session=True)")
        child = ("import subprocess,sys,time; "
                 f"subprocess.run([sys.executable, '-c', {intermediate!r}], check=True); time.sleep(60)")
        lines = []
        rc, err = run_stream(StreamRequest([sys.executable, "-c", child], os.getcwd(), dict(os.environ), 2.5),
                             lines.append)
        self.assertEqual(rc, 124)
        self.assertEqual(lines, ['reparented to 1\n'], 'the parent chain must be gone before the timeout')
        pids = [int(p) for p in pidfile.read_text().split()]
        self.assertEqual(len(pids), 1)
        deadline = time.monotonic() + 2
        while alive(pids[0]) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(alive(pids[0]), "the process the role left behind survived the timeout")
        self.assertIn("Stopped 1 process(es)", err)

    def test_descendant_that_changed_session_is_stopped_through_the_parent_chain(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        pidfile = Path(folder.name) / "pids"
        self.addCleanup(stop_fixture_children, pidfile)
        detached = f"import os,time;open({str(pidfile)!r},'w').write(str(os.getpid()));time.sleep(60)"
        child = textwrap.dedent(f"""
            import os, subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", {detached!r}], start_new_session=True, env={{"PATH": os.environ["PATH"]}})
            time.sleep(60)
        """)
        rc, _ = run_stream(StreamRequest([sys.executable, "-c", child], os.getcwd(), dict(os.environ), 1.5),
                           lambda line: None)
        self.assertEqual(rc, 124)
        pids = [int(p) for p in pidfile.read_text().split()]
        time.sleep(0.3)
        self.assertTrue(pids and not any(alive(p) for p in pids))

    def test_normal_exit_stops_nothing_and_reports_nothing(self):
        rc, err = run_stream(StreamRequest([sys.executable, "-c", "print('ok')"], os.getcwd(), dict(os.environ), 5),
                             lambda line: None)
        self.assertEqual(rc, 0)
        self.assertNotIn("Stopped", err)

    def test_the_sweep_never_touches_the_controller_or_its_parent(self):
        self.assertEqual(process_io.kill_pids(dict.fromkeys((os.getpid(), os.getppid(), 0, 1))), 0)

    def test_the_sweep_matches_environment_entries_and_leaves_unrelated_commands_alive(self):
        tag = uuid.uuid4().hex
        code = 'import time;time.sleep(60)'
        tagged_child = subprocess.Popen([sys.executable, '-c', code],
                                        env={**os.environ, process_io.ROLE_TAG: tag})
        self.addCleanup(tagged_child.wait)
        self.addCleanup(tagged_child.kill)
        unrelated = subprocess.Popen([sys.executable, '-c', code, f'{process_io.ROLE_TAG}={tag}'],
                                     env={**os.environ, process_io.ROLE_TAG: tag + '-different'})
        self.addCleanup(unrelated.wait)
        self.addCleanup(unrelated.kill)
        found = process_io.tagged(tag)
        self.assertEqual(set(found), {tagged_child.pid})
        self.assertEqual(process_io.kill_pids(found), 1)
        tagged_child.wait(timeout=2)
        self.assertEqual(tagged_child.returncode, -signal.SIGKILL)
        self.assertIsNone(unrelated.poll(), 'command text and a different tag must not authorize a kill')

    def test_the_sweep_does_not_kill_a_reused_pid(self):
        with patch.object(process_io, 'process_identity', return_value=('new-process',), create=True), \
                patch.object(process_io.os, 'kill') as kill:
            self.assertEqual(process_io.kill_pids({1234567: ('old-process',)}), 0)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
