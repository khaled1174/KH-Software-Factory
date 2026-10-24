"""Provider outages are waited out in the same session; other failures stop the run."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import test_start as startup
sys.path.insert(0, str(startup.ADWS))
from adw_modules import agent_cc, transient
from adw_modules.data_types import CCRequest, KHConfig
from adw_modules.process_io import RoleTimeout


class RetryRunTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke
    unchanged = startup.StartupTests.unchanged

    def run_with(self, mode, failures, retries=None):
        self.cfg['execution'] = {'transient_retry_delay_seconds': 1}
        if retries is not None:
            self.cfg['execution']['transient_retries'] = retries
        self.config.write_text(json.dumps(self.cfg))
        self.env['KH_FIXTURE_FAILURES'] = str(failures)
        return self.invoke(mode)

    def budget(self):
        return json.loads((self.root/'adws/adw_data/sessions/guarded/execution_budget-task.json').read_text())

    def retry_events(self):
        db = next((self.root/'adws/adw_data').rglob('*.db'))
        with sqlite3.connect(db) as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "select payload_json from events where name = 'transient_retry' order by rowid")]

    def test_outage_on_plan_review_is_retried_and_the_task_completes(self):
        result = self.run_with('outage-plan_reviewer', 2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(), 'value = 2\n')
        self.assertEqual(self.budget()['transient_retries'], 2)
        # The plan reviewer runs twice: context review (bootstrap budget) and plan review.
        events = self.retry_events()
        self.assertEqual([e['retry'] for e in events], [1, 2, 1, 2])
        self.assertEqual([e['wait_seconds'] for e in events], [1, 2, 1, 2])
        self.assertTrue(all(e['agent'] == 'plan_reviewer' and '503' in e['reason'] for e in events), events)

    def test_retries_spend_no_model_send_or_correction_budget(self):
        clean = self.run_with('good', 0)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        baseline = self.budget()
        self.tearDown(); self.setUp()
        result = self.run_with('outage-builder', 3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = self.budget()
        self.assertEqual(after['transient_retries'], 3)
        self.assertEqual((after['model_sends'], after['output_corrections']),
                         (baseline['model_sends'], baseline['output_corrections']))

    def test_outage_beyond_the_retry_limit_stops_and_says_so(self):
        result = self.run_with('outage-builder', 5, retries=2)
        self.assertNotEqual(result.returncode, 0)
        self.unchanged()
        self.assertIn('provider outage persisted after 2 retries', result.stdout + result.stderr)
        self.assertEqual(self.budget()['transient_retries'], 2)

    def test_usage_limit_is_not_waited_out(self):
        result = self.run_with('quota-planner', 1)
        self.assertNotEqual(result.returncode, 0)
        self.unchanged()
        self.assertEqual(self.budget()['transient_retries'], 0)
        self.assertEqual(self.retry_events(), [])

    def test_zero_retries_turns_the_retry_off(self):
        result = self.run_with('outage-planner', 1, retries=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.retry_events(), [])


class ClassifyTests(unittest.TestCase):
    def test_request_aborted_before_any_reply_is_transient(self):
        self.assertEqual(transient.classify('Operation was aborted', None).lower(),
                         'operation was aborted')

    def test_role_timeout_is_not_an_outage_even_with_transient_details(self):
        self.assertEqual(transient.classify(RoleTimeout(
            'Antigravity timed out after 1800.0s: 429 Too Many Requests', None)), '')
        folder = self.write(['stream disconnected'], name='relay.stderr.txt')
        self.assertEqual(transient.classify(RoleTimeout('Delegate supervisor timed out', None), folder), '')

    def write(self, lines, name='raw_output.jsonl'):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(folder, ignore_errors=True))
        (folder/name).write_text('\n'.join(json.dumps(l) if isinstance(l, dict) else l for l in lines) + '\n')
        return folder

    def test_provider_outages_from_each_runtime_are_transient(self):
        cases = {
            'claude': [{'type': 'result', 'is_error': True, 'api_error_status': 503,
                        'result': 'API Error: 503 Service Unavailable. This is a server-side issue'}],
            'claude-overloaded': [{'type': 'result', 'is_error': True, 'api_error_status': 529,
                                   'result': 'API Error: 529 {"type":"overloaded_error","message":"Overloaded"}'}],
            'codex': [{'type': 'error', 'message': 'Reconnecting... 5/5 (unexpected status 503 Service Unavailable)'},
                      {'type': 'turn.failed', 'error': {'message': 'Selected model is at capacity. Please try a different model.'}}],
            'pi': [{'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'error',
                                                       'errorMessage': 'fetch failed', 'content': []}}],
            'gemini': [{'type': 'error', 'severity': 'error', 'message': '[API Error: The model is overloaded. Please try again later.]'}],
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.assertTrue(transient.classify('transport failed', self.write(lines)))
        self.assertTrue(transient.classify('gemini exited 1: ... {\n  status: 503\n}', None))
        self.assertTrue(transient.classify('Delegate transport failed: [Errno 104] ECONNRESET', None))

    def test_real_failures_are_not_transient(self):
        for text in ('claude exited 124: ', 'API Error: 401 invalid x-api-key',
                     'API Error: 429 Claude AI usage limit reached', 'You exceeded your current quota (429)',
                     'Rate limit: insufficient_quota', 'prompt is too long: 250000 tokens',
                     'builder failed gates after 2 attempt(s)', 'Relay receipt does not match requested cwd'):
            with self.subTest(text):
                self.assertEqual(transient.classify(text, None), '')

    def test_model_prose_about_503_does_not_make_a_failure_transient(self):
        folder = self.write([{'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Return 503 Service Unavailable when the store is overloaded.'}]}}])
        self.assertEqual(transient.classify('claude exited 124: ', folder), '')

    def test_backoff_doubles(self):
        self.assertEqual([transient.delay(n, 60) for n in (1, 2, 3)], [60, 120, 240])

    def test_defaults(self):
        settings = KHConfig().execution
        self.assertEqual((settings.transient_retries, settings.transient_retry_delay_seconds), (3, 60))


class SessionAlreadyExistsTests(unittest.TestCase):
    """A first send that failed after the CLI created its session is continued, not recreated."""

    def request(self, folder):
        return CCRequest(prompt='go', system_prompt='s', model='sonnet', thinking='high', session_id='00000000-0000-4000-8000-000000000001',
                         session_dir=str(folder/'sessions'), raw_output_path=str(folder/'raw.jsonl'), tools=None,
                         cwd=str(folder), timeout_seconds=30)

    def check(self, module, message):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(folder, ignore_errors=True))
        calls = []

        def fake(request, resume, *rest, **kw):
            calls.append(resume)
            result = module.CCRunResult(session_id=request.session_id)
            if not resume:
                result.returncode, result.stderr_tail = 1, message
            else:
                result.returncode, result.terminal_success = 0, True
            return result
        with patch.object(module, '_run_once', side_effect=fake):
            module.run(self.request(folder))
        self.assertEqual(calls, [False, True])

    def test_claude(self):
        self.check(agent_cc, 'Error: Session ID 00000000-0000-4000-8000-000000000001 is already in use.')



if __name__ == '__main__':
    unittest.main()
