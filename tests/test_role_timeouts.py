"""Only supervisor cutoffs are recoverable role timeouts; partial usage survives."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_cc, process_io
from adw_modules.activity import TIMEOUT_MARK
from adw_modules.data_types import CCRequest


class RoleTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cli = self.root / 'fake-agent'

    def invoke(self, adapter, mode):
        events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'partial work'}],
                                             'usage': {'input_tokens': 4, 'output_tokens': 6}}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'partial work',
                'total_cost_usd': 1.25, 'status': 'success', 'stats': {'input': 4, 'output_tokens': 6}},
        ]
        script = ('#!' + sys.executable + '\nimport json,sys,time\n' +
                  '\n'.join(f'print({json.dumps(event)!r},flush=True)' for event in events) +
                  ('\ntime.sleep(60)\n' if mode == 'cutoff' else '\nsys.exit(124)\n'))
        self.cli.write_text(script)
        self.cli.chmod(0o700)
        request_type = CCRequest
        request = request_type(prompt='work', system_prompt='builder', model='fixture/model',
            session_id='role', session_dir=str(self.root / 'sessions'),
            raw_output_path=str(self.root / adapter.__name__ / 'raw.jsonl'), cwd=str(self.root),
            tools=None, timeout_seconds=1)
        path_name = 'CC_PATH'
        model = 'fixture-model'
        with patch.object(adapter, path_name, str(self.cli)), \
                patch.object(adapter, 'resolve_model', return_value=model), \
                patch.object(adapter, 'context_window', return_value=10000):
            return adapter.run(request)

    def test_supervisor_distinguishes_its_cutoff_from_exit_124(self):
        cutoff = process_io.run_stream(process_io.StreamRequest(
            [sys.executable, '-c', 'import time;time.sleep(60)'], str(self.root), dict(os.environ), .2),
            lambda line: None)
        ordinary = process_io.run_stream(process_io.StreamRequest(
            [sys.executable, '-c', 'raise SystemExit(124)'], str(self.root), dict(os.environ), 2),
            lambda line: None)
        self.assertEqual(cutoff[0], 124)
        self.assertEqual(ordinary[0], 124)
        self.assertTrue(getattr(cutoff, 'timed_out', False), 'the supervisor must identify its own cutoff')
        self.assertFalse(getattr(ordinary, 'timed_out', False))
        code, stderr = cutoff  # Existing two-value callers retain their contract.
        self.assertEqual(code, 124)
        self.assertIn('Timed out after', stderr)

    def test_supervisor_marks_its_cutoff_after_output_streams_close(self):
        result = process_io.run_stream(process_io.StreamRequest(
            [sys.executable, '-c', 'import os,time;os.close(1);os.close(2);time.sleep(60)'],
            str(self.root), dict(os.environ), .2), lambda line: None)
        self.assertEqual(result[0], 124)
        self.assertTrue(getattr(result, 'timed_out', False))

    def test_native_adapters_raise_typed_timeout_with_partial_usage(self):
        for adapter in (agent_cc,):
            with self.subTest(adapter=adapter.__name__):
                with self.assertRaises(RuntimeError) as caught:
                    self.invoke(adapter, 'cutoff')
                error = caught.exception
                self.assertTrue(hasattr(error, 'partial_result'), 'partial usage must survive the cutoff')
                self.assertIsInstance(error, process_io.RoleTimeout)
                partial = error.partial_result
                self.assertTrue(partial.timed_out)
                self.assertEqual(partial.returncode, 124)
                self.assertFalse(partial.terminal_success)
                self.assertFalse(partial.cost_known)
                self.assertEqual(partial.tokens, 10)
                self.assertEqual((partial.cost, partial.usage.total_cost), (1.25, 1.25))
                self.assertIn(TIMEOUT_MARK, str(error))

    def test_native_exit_124_is_an_ordinary_failure(self):
        for adapter in (agent_cc,):
            with self.subTest(adapter=adapter.__name__):
                with self.assertRaises(RuntimeError) as caught:
                    self.invoke(adapter, 'exit')
                self.assertFalse(hasattr(caught.exception, 'partial_result'))
                self.assertNotIn(TIMEOUT_MARK, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
