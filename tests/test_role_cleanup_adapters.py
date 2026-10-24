"""Native adapters keep partial receipts and never recover across failed cleanup."""
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_cc, infrastructure, process_io
from adw_modules.data_types import CCRequest


def cleanup_receipt(complete):
    return {'complete': complete, 'root_reaped': True,
            'surviving_pids': [] if complete else [987654321],
            'stopped_descendants': 0, 'workers_stopped': True, 'open_streams': []}


@contextmanager
def raw_close_fault():
    """Raise from close after closing the actual append handle, not only __exit__."""
    original_open = Path.open
    handles = []
    failure = OSError('fixture raw evidence close failed')
    class FaultyClose:
        def __init__(self, raw):
            self.raw = raw
        def __getattr__(self, name):
            return getattr(self.raw, name)
        def close(self):
            self.raw.close()
            raise failure
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    def open_raw(path, *args, **kwargs):
        raw = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get('mode', 'r')
        if path.name == 'raw.jsonl' and mode == 'a':
            handles.append(raw)
            return FaultyClose(raw)
        return raw
    try:
        with patch.object(Path, 'open', open_raw):
            yield handles, failure
    finally:
        for raw in handles:
            if not raw.closed:
                raw.close()


def native_events(adapter):
    return [
        {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'partial work'}],
                                         'usage': {'input_tokens': 4, 'output_tokens': 6}}},
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'result': 'partial work', 'total_cost_usd': 1.25},
    ]


class NativeAdapterCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kh-role-adapters-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.case_number = 0
        self.owner_permissions = {'allow': ['Read(src/**)'], 'deny': ['Bash(rm *)']}

    def invoke(self, adapter, *, returncode=0, timed_out=False, complete=False,
               stderr='', existing_session=False, callback_error=None):
        self.case_number += 1
        self.existing_session = existing_session
        folder = self.folder / str(self.case_number)
        folder.mkdir()
        private = folder / 'private'
        private.mkdir(mode=0o700)
        request_type = CCRequest
        request = request_type(prompt='work', system_prompt='builder', model='fixture-model',
            session_id='role', session_dir=str(folder / 'sessions'),
            raw_output_path=str(folder / 'attempt/raw.jsonl'), cwd=str(folder),
            tools=['read'], timeout_seconds=10)
        self.last_request, self.private = request, private
        self.marker = adapter._session_marker(request)
        if existing_session:
            self.marker.write_bytes(b'owner recorded session\n')
        stream = process_io.StreamResult(returncode, stderr, timed_out=timed_out,
                                        cleanup=cleanup_receipt(complete))
        self.last_stream = stream
        self.sent = []
        self.created_holders, self.dispatched_files = [], {}
        mkdtemp = tempfile.mkdtemp
        def owned_holder(*args, **kwargs):
            kwargs['dir'] = folder
            holder = mkdtemp(*args, **kwargs)
            self.created_holders.append(Path(holder))
            return holder
        def deliver(stream_request, receive, **kwargs):
            self.sent.append(stream_request)
            for flag in ('--settings',):
                if flag in stream_request.argv:
                    path = Path(stream_request.argv[stream_request.argv.index(flag) + 1])
                    self.dispatched_files[path] = path.read_bytes()
            for event in native_events(adapter):
                receive(json.dumps(event) + '\n')
            return stream
        def observe(event):
            if callback_error is not None:
                raise callback_error
        model = 'fixture-model'
        with ExitStack() as stack:
            stack.enter_context(patch.object(adapter, 'resolve_model', return_value=model))
            stack.enter_context(patch.object(adapter, 'context_window', return_value=10000))
            stack.enter_context(patch.object(process_io, 'run_stream', side_effect=deliver))
            stack.enter_context(patch.object(subprocess, 'Popen',
                side_effect=AssertionError('native adapter seam must not launch any process')))
            stack.enter_context(patch.object(adapter, 'owner_permissions', return_value=self.owner_permissions))
            stack.enter_context(patch.object(adapter, 'isolated', return_value=True))
            stack.enter_context(patch.object(infrastructure, 'claude_deny_rules', return_value=[]))
            # The original function is captured before patching the shared
            # tempfile module, so every retained holder stays fixture-owned.
            stack.enter_context(patch.object(adapter.tempfile, 'mkdtemp', side_effect=owned_holder))
            return adapter.run(request, on_event=observe)

    def assert_cleanup_failure(self, error, adapter, returncode, timed_out):
        self.assertIsInstance(error, process_io.RoleCleanupError)
        self.assertNotIsInstance(error, process_io.RoleTimeout)
        self.assertIs(error.result, error.partial_result)
        self.assertFalse(error.resume_safe)
        self.assertEqual(error.timed_out, timed_out)
        partial = error.partial_result
        self.assertEqual((partial.returncode, partial.timed_out), (returncode, timed_out))
        self.assertFalse(partial.terminal_success)
        self.assertFalse(partial.cost_known)
        self.assertEqual((partial.tokens, partial.usage.total_tokens), (10, 10))
        # A pre-existing native session without an accounting baseline cannot
        # attribute its cumulative price to this send. Cleanup rules are unchanged.
        imported_claude = adapter is agent_cc and self.existing_session
        expected_cost = 0 if imported_claude else 1.25
        self.assertEqual((partial.cost, partial.usage.total_cost), (expected_cost, expected_cost))
        self.assertEqual(partial.text, 'partial work')
        self.assertFalse(error.kh_cleanup['complete'])
        self.assertEqual(len(self.sent), 1, 'failed cleanup must never dispatch another model call')

    def test_incomplete_cleanup_keeps_usage_and_native_exit_facts(self):
        for adapter in (agent_cc,):
            for returncode, timed_out in ((0, False), (124, False), (124, True)):
                with self.subTest(adapter=adapter.__name__, code=returncode, timed_out=timed_out):
                    with self.assertRaises(process_io.RoleCleanupError) as caught:
                        self.invoke(adapter, returncode=returncode, timed_out=timed_out)
                    self.assert_cleanup_failure(caught.exception, adapter, returncode, timed_out)

    def test_session_recovery_text_cannot_trigger_fallback_after_failed_cleanup(self):
        cases = ((agent_cc, True, 'No conversation found'),
                 (agent_cc, False, 'session already in use'))
        for adapter, existing, warning in cases:
            with self.subTest(adapter=adapter.__name__, warning=warning):
                with self.assertRaises(process_io.RoleCleanupError) as caught:
                    self.invoke(adapter, returncode=1, stderr=warning, existing_session=existing)
                self.assert_cleanup_failure(caught.exception, adapter, 1, False)
                self.assertEqual(self.marker.exists(), existing)
                if existing:
                    self.assertEqual(self.marker.read_bytes(), b'owner recorded session\n')


    def test_claude_retains_in_use_native_settings_bytes_until_cleanup_is_confirmed(self):
        with self.assertRaises(process_io.RoleCleanupError) as caught:
            self.invoke(agent_cc)
        error = caught.exception
        retained = Path(error.retained_policy_directory)
        self.assertEqual(self.created_holders, [retained])
        self.assertTrue(retained.is_relative_to(self.folder))
        self.assertIn(str(retained), error.kh_cleanup['retained_paths'])
        settings = retained / 'settings.json'
        self.assertEqual(settings.read_bytes(), self.dispatched_files[settings])
        self.assertEqual(json.loads(settings.read_bytes())['permissions'], {
            **self.owner_permissions, 'deny': [*self.owner_permissions['deny'],
                'Read(~/.ssh)', 'Read(~/.ssh/**)', 'Read(~/.aws)', 'Read(~/.aws/**)',
                'Read(~/.config/gh)', 'Read(~/.config/gh/**)']})
        evidence = Path(self.last_request.raw_output_path).with_name('claude-settings.json')
        self.assertEqual(evidence.read_bytes(), self.dispatched_files[settings])
        command = self.sent[0].argv
        self.assertEqual(command[command.index('--settings') + 1], str(settings))

    def test_callback_failure_with_incomplete_cleanup_retains_native_policy_holders(self):
        for adapter in (agent_cc,):
            with self.subTest(adapter=adapter.__name__):
                failure = RuntimeError('fixture callback failed while owned process may remain')
                # Real callback annotation and signal behavior are exercised by
                # test_role_cleanup; this is its unchanged adapter boundary.
                failure.kh_cleanup = cleanup_receipt(False)
                with self.assertRaises(RuntimeError) as caught:
                    self.invoke(adapter, callback_error=failure)
                self.assertIs(caught.exception, failure)
                retained = Path(failure.retained_policy_directory)
                self.assertTrue(retained.is_dir())
                self.assertTrue(retained.is_relative_to(self.folder))
                self.assertIn(str(retained), failure.kh_cleanup['retained_paths'])
                self.assertTrue(self.dispatched_files)
                for path, original in self.dispatched_files.items():
                    self.assertTrue(path.is_relative_to(retained))
                    self.assertEqual(path.read_bytes(), original)
                self.assertEqual(len(self.sent), 1)

    def test_complete_cleanup_keeps_normal_success_behavior(self):
        for adapter in (agent_cc,):
            with self.subTest(adapter=adapter.__name__):
                result = self.invoke(adapter, complete=True)
                self.assertEqual((result.returncode, result.timed_out), (0, False))
                self.assertTrue(result.terminal_success)
                self.assertEqual(result.tokens, 10)
                self.assertEqual(len(self.sent), 1)
                self.assertEqual((result.cost, result.cost_known), (1.25, True))
                if adapter is agent_cc:
                    self.assertEqual(len(self.created_holders), 1)
                    self.assertFalse(self.created_holders[0].exists())

    def test_complete_cleanup_keeps_supervisor_timeout_distinct(self):
        for adapter in (agent_cc,):
            with self.subTest(adapter=adapter.__name__):
                with self.assertRaises(process_io.RoleTimeout) as caught:
                    self.invoke(adapter, returncode=124, timed_out=True, complete=True)
                self.assertNotIsInstance(caught.exception, process_io.RoleCleanupError)
                self.assertEqual(caught.exception.partial_result.tokens, 10)
                self.assertTrue(caught.exception.partial_result.timed_out)
                self.assertEqual(len(self.sent), 1)

    def test_raw_close_failure_cannot_mask_incomplete_cleanup_receipt(self):
        for adapter in (agent_cc,):
            for timed_out in (False, True):
                with self.subTest(adapter=adapter.__name__, timed_out=timed_out), raw_close_fault() as (handles, _):
                    with self.assertRaises(process_io.RoleCleanupError) as caught:
                        self.invoke(adapter, returncode=124, timed_out=timed_out)
                    self.assert_cleanup_failure(caught.exception, adapter, 124, timed_out)
                    self.assertEqual(len(handles), 1)
                    self.assertTrue(handles[0].closed, 'the actual evidence file must already be closed')

    def test_raw_close_failure_preserves_primary_callback_error_or_interrupt(self):
        for adapter in (agent_cc,):
            for error_type in (RuntimeError, KeyboardInterrupt):
                with self.subTest(adapter=adapter.__name__, error=error_type.__name__), \
                     raw_close_fault() as (handles, _):
                    failure = error_type('fixture primary callback cancellation')
                    failure.kh_cleanup = cleanup_receipt(False)
                    with self.assertRaises(error_type) as caught:
                        self.invoke(adapter, callback_error=failure)
                    self.assertIs(caught.exception, failure)
                    self.assertFalse(failure.kh_cleanup['complete'])
                    self.assertEqual(len(handles), 1)
                    self.assertTrue(handles[0].closed)
                    self.assertEqual(len(self.sent), 1)

    def test_raw_close_failure_without_primary_error_still_propagates(self):
        for adapter in (agent_cc,):
            with self.subTest(adapter=adapter.__name__), raw_close_fault() as (handles, failure):
                with self.assertRaises(OSError) as caught:
                    self.invoke(adapter, complete=True)
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(handles), 1)
                self.assertTrue(handles[0].closed)
                self.assertEqual(len(self.sent), 1)




if __name__ == '__main__':
    unittest.main()
