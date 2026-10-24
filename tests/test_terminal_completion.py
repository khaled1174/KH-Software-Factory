"""A terminal provider result requires drained pipes and stable final evidence."""
import json
import os
from pathlib import Path
import signal
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_cc, agents, permissions, process_io
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams
from tests import test_core as core_fixtures
from tests import test_role_cleanup as cleanup_fixtures


class TerminalSupervisorTests(unittest.TestCase):
    # Reuse fixture ownership/cleanup, not the original class's test methods.
    setUp = cleanup_fixtures.RoleCleanupTests.setUp
    remember_children = cleanup_fixtures.RoleCleanupTests.remember_children
    kill_fixture_processes = cleanup_fixtures.RoleCleanupTests.kill_fixture_processes
    cleanup_resources = cleanup_fixtures.RoleCleanupTests.cleanup_resources
    running = staticmethod(cleanup_fixtures.RoleCleanupTests.running)
    request = cleanup_fixtures.RoleCleanupTests.request
    drive = cleanup_fixtures.RoleCleanupTests.drive
    assert_stopped_and_closed = cleanup_fixtures.RoleCleanupTests.assert_stopped_and_closed

    def terminal_run(self, mode='closed_child', *, input_text='', timeout=5):
        watch = process_io.TerminalCompletion(grace_seconds=.15)
        request = self.request(mode, input_text=input_text)
        request.timeout = timeout
        request.terminal_completion = watch
        lines = []
        def receive(line):
            lines.append(line)
            if line == 'READY\n':
                watch.mark()
        start = time.monotonic()
        result = self.drive(request, receive)
        return result, lines, time.monotonic() - start

    def assert_completion(self, result, termination):
        self.assertEqual(result.completion['termination'], termination)
        self.assertTrue(result.completion['stdin_closed'])
        self.assertTrue(result.completion['stream_drained'])
        self.assertEqual(result.completion['grace_seconds'], .15)
        self.assertEqual(result.completion['native_returncode'], self.primary[0].returncode)
        self.assertTrue(result.cleanup['complete'])
        self.assert_stopped_and_closed()

    def test_terminal_hanging_root_stops_detached_child_and_all_readers(self):
        result, lines, elapsed = self.terminal_run()
        self.assertEqual(lines, ['READY\n'])
        self.assertLess(elapsed, 4)
        self.assertFalse(result.timed_out)
        self.assertIn(result[0], (-signal.SIGTERM, -signal.SIGKILL))
        self.assert_completion(result, 'controller_stopped_after_result')
        self.assertGreaterEqual(result.completion['grace_elapsed_seconds'], .15)

    def test_natural_zero_exit_still_cleans_detached_child_with_closed_pipes(self):
        result, _, _ = self.terminal_run('exit_0')
        self.assertEqual(result[0], 0)
        self.assertFalse(result.timed_out)
        self.assert_completion(result, 'exited')
        self.assertGreaterEqual(result.cleanup['stopped_descendants'], 1)

    def test_closed_output_pipes_do_not_hide_a_hanging_root(self):
        source = self.fixture.read_text().replace(
            "(folder/'ready').touch()", "(folder/'ready').touch()\nos.close(1);os.close(2)")
        self.fixture.write_text(source)
        result, _, elapsed = self.terminal_run()
        self.assertLess(elapsed, 4)
        self.assertFalse(result.timed_out)
        self.assert_completion(result, 'controller_stopped_after_result')

    def test_natural_nonzero_is_preserved_without_supervisor_success(self):
        result, _, _ = self.terminal_run('exit_7')
        self.assertEqual(result[0], 7)
        self.assertFalse(result.timed_out)
        self.assert_completion(result, 'exited')

    def test_sigterm_output_and_stderr_are_drained_before_return(self):
        # Ownership cleanup may signal the process through both its group and
        # tracked PID. Deliberately repeat the signal inside the handler so the
        # fixture tests pipe drainage without Python buffered-I/O reentrancy.
        handler = (
            "def terminating(signum, frame):\n"
            " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            " os.kill(os.getpid(), signal.SIGTERM)\n"
            " os.kill(os.getpid(), signal.SIGTERM)\n"
            " os.write(1, b'AFTER_TERMINAL\\n')\n"
            " os.write(2, b'permission denied during shutdown\\n')\n"
            " raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, terminating)\n"
        )
        self.fixture.write_text(self.fixture.read_text().replace("print('READY',flush=True)",
                                                                  handler + "print('READY',flush=True)"))
        result, lines, _ = self.terminal_run()
        self.assertEqual(lines, ['READY\n', 'AFTER_TERMINAL\n'])
        self.assertIn('permission denied during shutdown', result[1])
        self.assertNotIn('Traceback', result[1])
        self.assert_completion(result, 'controller_stopped_after_result')

    def test_unclosed_blocked_input_cannot_trigger_terminal_grace(self):
        result, _, _ = self.terminal_run('stdin_holder', input_text='x' * (16 << 20), timeout=2)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.completion['termination'], 'timeout')
        self.assert_stopped_and_closed()

    def test_uncertain_cleanup_is_not_hidden_by_terminal_result(self):
        stop = process_io.CommandProcessScope.stop
        def uncertain(scope, *args, **kwargs):
            state = stop(scope, *args, **kwargs)
            return {**state, 'complete': False}
        with patch.object(process_io.CommandProcessScope, 'stop', uncertain):
            result, _, _ = self.terminal_run()
        self.assertFalse(result.cleanup['complete'])
        self.assert_stopped_and_closed()

    def test_missing_terminal_result_keeps_the_role_timeout(self):
        request = self.request(input_text='')
        request.timeout = 2
        request.terminal_completion = process_io.TerminalCompletion(grace_seconds=.15)
        result = self.drive(request, lambda line: None)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.completion['termination'], 'timeout')
        self.assert_stopped_and_closed()




class ControllerTerminalCompletionTests(unittest.TestCase):
    tearDown = core_fixtures.CoreTests.tearDown

    def setUp(self):
        core_fixtures.CoreTests.setUp(self)
        self.agent.coding_agent = 'claude_code'
        self.agent.model = 'fixture'
        self.agent.tools = ['read', 'write']

    @staticmethod
    def receipt(text):
        return agent_cc.CCRunResult(terminal_success=True, text=text, returncode=0)

    def call(self, responder):
        with patch.object(agent_cc, 'run', side_effect=responder):
            with self.run.phase(PhaseParams(name='build', kind='agent', owner='builder',
                    description='Verify normal completion against final controller gates', retries=0)) as phase:
                return phase.call(AgentCall(output_type=BuildOutput, prompt='implement value 2'))

    def successful_worker(self, request, **kwargs):
        (Path(request.cwd) / 'source.py').write_text('value = 2\n')
        return self.receipt(json.dumps({'status': 'success', 'changed_files': ['source.py']}))

    def test_normal_completion_promotes_only_after_final_changed_file_gate(self):
        result = self.call(self.successful_worker)
        self.assertEqual(result.changed_files, ['source.py'])
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 2\n')
        rows = [row for phase in self.run.gate_history_by_phase.values() for row in phase]
        self.assertTrue(any(row['gate'] == 'diff_matches_claims' and row['final'] == 'passed'
                            for row in rows))

    def test_normal_completion_cannot_bypass_incorrect_final_changed_file_claims(self):
        calls = []
        def wrong_claim(request, **kwargs):
            calls.append(request)
            result = self.successful_worker(request, **kwargs)
            result.text = json.dumps({'status': 'success', 'changed_files': []})
            return result
        with self.assertRaises(agents.GateFailure):
            self.call(wrong_claim)
        self.assertGreater(len(calls), 1, 'existing bounded report correction must still run')
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 1\n')

    def test_normal_completion_cannot_bypass_final_permission_snapshot(self):
        def forbidden_write(request, **kwargs):
            result = self.successful_worker(request, **kwargs)
            (Path(request.cwd) / 'outside-scope.txt').write_text('must not promote')
            return result
        with self.assertRaises(permissions.PermissionBreach):
            self.call(forbidden_write)
        self.assertFalse((self.root / 'outside-scope.txt').exists())
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 1\n')

    def test_first_send_timeout_preserves_the_original_failure(self):
        failure = process_io.RoleTimeout('fixture first-send timeout',
            agent_cc.CCRunResult(timed_out=True, returncode=124))
        def timeout(request, **kwargs):
            (Path(request.cwd) / 'source.py').write_text('unfinished')
            raise failure
        with self.assertRaises(process_io.RoleTimeout) as caught:
            self.call(timeout)
        self.assertIs(caught.exception, failure)
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 1\n')

    def test_normal_completion_retains_normal_malformed_json_correction(self):
        calls = []
        def corrected(request, **kwargs):
            calls.append(request)
            result = self.successful_worker(request, **kwargs)
            if len(calls) == 1:
                result.text = 'not a JSON report'
            return result
        result = self.call(corrected)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.changed_files, ['source.py'])
        self.assertEqual((self.root / 'source.py').read_text(), 'value = 2\n')


if __name__ == '__main__':
    unittest.main()
