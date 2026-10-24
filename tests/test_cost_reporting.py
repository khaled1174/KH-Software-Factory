"""Cost reporting distinguishes known zero, partial totals and interrupted calls."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_core as core
from adw_modules import agent_cc, process_io
from adw_modules.data_types import AgentCall, BuildOutput, CCRequest, PhaseParams


class CostPhaseTests(unittest.TestCase):
    setUp = core.CoreTests.setUp
    tearDown = core.CoreTests.tearDown
    call = core.CoreTests.call

    def session_cost(self):
        return self.tracer.conn.execute(
            "SELECT total_cost,cost_known FROM sessions WHERE adw_id='probe'").fetchone()

    def test_joined_run_preserves_known_subtotal_and_unknown_history(self):
        from adw_modules.runner import Run
        self.run.add_usage(10, 1.25, False)
        resumed = Run(self.run.cfg, 'probe', self.tracer, 'fixture')
        self.assertEqual((resumed.tokens, resumed.cost, resumed.cost_known), (10, 1.25, False))
        resumed.add_usage(5, 0.0, True)
        self.assertEqual((resumed.tokens, resumed.cost, resumed.cost_known), (15, 1.25, False))
        self.assertEqual(self.session_cost(), (1.25, 0))

    def test_transport_timeout_marks_session_total_unknown(self):
        def timeout(request, **kwargs):
            raise TimeoutError('Dispatched provider timed out')
        with self.assertRaises(TimeoutError):
            self.call(timeout)
        self.assertEqual(self.session_cost(), (0.0, 0))
        self.assertFalse(self.run.cost_known)

    def test_transport_cancellation_marks_session_total_unknown(self):
        def cancelled(request, **kwargs):
            raise KeyboardInterrupt('Cancelled dispatched provider')
        with self.assertRaises(KeyboardInterrupt):
            self.call(cancelled)
        self.assertEqual(self.session_cost(), (0.0, 0))
        self.assertFalse(self.run.cost_known)

    def test_successful_known_zero_is_still_known(self):
        def success(request, **kwargs):
            (Path(request.cwd) / 'source.py').write_text('value = 2\n')
            return agent_cc.CCRunResult(terminal_success=True, cost=0.0, cost_known=True,
                text=json.dumps({'status': 'success', 'summary': 'Changed value', 'changed_files': ['source.py']}))
        self.call(success)
        self.assertEqual(self.session_cost(), (0.0, 1))

    def test_interrupted_later_call_preserves_previously_known_subtotal(self):
        def success(request, **kwargs):
            (Path(request.cwd) / 'source.py').write_text('value = 2\n')
            result = agent_cc.CCRunResult(terminal_success=True, cost=1.25, cost_known=True,
                text=json.dumps({'status': 'success', 'summary': 'Changed value', 'changed_files': ['source.py']}))
            result.usage.total_cost = 1.25
            return result
        self.call(success, name='first')
        self.assertEqual(self.session_cost(), (1.25, 1))
        def timeout(request, **kwargs):
            raise TimeoutError('Later provider timed out')
        with self.assertRaises(TimeoutError):
            self.call(timeout, name='second')
        self.assertEqual(self.session_cost(), (1.25, 0))

    def test_permission_failure_cannot_hide_unknown_transport_cost(self):
        def timeout(request, **kwargs):
            (Path(request.cwd) / 'prohibited.txt').write_text('unauthorized')
            raise TimeoutError('Provider timed out after a write')
        with self.assertRaises(core.permissions.PermissionBreach):
            self.call(timeout)
        self.assertEqual(self.session_cost(), (0.0, 0))

    def test_nonfinite_usage_does_not_enter_phase_or_session_totals(self):
        def success(request, **kwargs):
            (Path(request.cwd) / 'source.py').write_text('value = 2\n')
            result = agent_cc.CCRunResult(terminal_success=True, cost=float('nan'), cost_known=True,
                text=json.dumps({'status':'success','changed_files':['source.py']}))
            for field, value in dict(input_cost=-1, output_cost=float('inf'), cache_read_cost=float('nan'),
                                     cache_write_cost=True, total_cost=float('nan')).items():
                setattr(result.usage, field, value)
            return result
        self.call(success)
        self.assertEqual(self.session_cost(), (0.0, 0))
        events = [json.loads(line) for line in self.tracer.events_jsonl.read_text().splitlines()]
        payload = next(event['payload'] for event in events if event['type'] == 'agent_end')
        self.assertFalse(payload['cost_known'])
        self.assertEqual(payload['cost'], 0)
        for name, value in payload['usage'].items():
            if name.endswith('_cost'):
                self.assertTrue(math.isfinite(value) and value >= 0, (name, value))


    def test_adapter_total_is_used_consistently_in_phase_and_session(self):
        def success(request, **kwargs):
            (Path(request.cwd) / 'source.py').write_text('value = 2\n')
            result = agent_cc.CCRunResult(terminal_success=True, cost=1.25, cost_known=True,
                text=json.dumps({'status': 'success', 'summary': 'Changed value', 'changed_files': ['source.py']}))
            result.usage.total_cost = float('nan')
            return result
        self.call(success)
        self.assertEqual(self.session_cost(), (1.25, 0))
        events = [json.loads(line) for line in self.tracer.events_jsonl.read_text().splitlines()]
        payload = next(event['payload'] for event in events if event['type'] == 'agent_end')
        self.assertEqual(payload['cost'], 1.25)
        self.assertEqual(payload['usage']['total_cost'], 1.25)


class AdapterCostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)






    def test_claude_invalid_cost_is_unknown_but_numeric_zero_is_known(self):
        request = CCRequest(prompt='x', system_prompt='x', model='sonnet', session_id='fixture',
                            session_dir=str(self.root), raw_output_path=str(self.root / 'cc.jsonl'))
        for value, known in ((0, True), (1.5, True), (None, False), (-1, False), (True, False),
                             (float('nan'), False), (float('inf'), False)):
            def stream(request, receive, **kwargs):
                receive(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'done',
                                    'total_cost_usd': value}))
                return 0, ''
            with self.subTest(value=value), patch.object(process_io, 'run_stream', side_effect=stream):
                result = agent_cc._run_once(request, False, None, None, None)
                self.assertEqual(result.cost_known, known)
                self.assertTrue(math.isfinite(result.cost) and result.cost >= 0)


if __name__ == '__main__':
    unittest.main()
