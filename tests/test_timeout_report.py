"""A timed-out role says so first and says what it was doing; the window is the CLI's.

Reproduces three things seen on a real project:

* a builder cut at its 2400 s limit while one test command had been running for
  38 minutes; the controller's message led with pi's unrelated "No project
  session found" warning, so every timeout read as something else;
* a timeout whose output also held an earlier aborted request was classified as a
  provider outage because of that abort;
* a Claude reviewer at 654,255 tokens was measured against an assumed 200K
  window (327%) although the CLI itself reports 1,000,000 (65%), which ended its
  task with SCOPE_TOO_LARGE_FOR_REVIEW.
"""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import activity, agent_cc, context_guard, transient  # noqa: E402
from adw_modules.activity import Activity, TIMEOUT_MARK, timeout_headline  # noqa: E402
from adw_modules.data_types import CCRequest  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def tool_start(call_id, command):
    return {'type':'assistant','message':{'content':[
        {'type':'tool_use','id':call_id,'name':'Bash','input':{'command':command}}]}}


def tool_end(call_id, command='x', tool='Bash', error=False):
    return {'type':'user','message':{'content':[
        {'type':'tool_result','tool_use_id':call_id,'content':command,'is_error':error}]}}


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.activity = Activity(clock=self.clock)

    def test_a_command_still_running_is_named_with_how_long_it_has_run(self):
        self.activity.observe(tool_start('1', 'swift test --package-path app'))
        self.clock.advance(38 * 60)
        text = self.activity.describe()
        self.assertIn('still running when cut: Bash: swift test --package-path app (for 38.0 min)', text)
        self.assertIn('1 tool call in this attempt', text)
        self.assertIn('no file written', text)

    def test_when_nothing_is_running_the_last_finished_call_and_last_write_are_named(self):
        self.activity.observe({'type':'assistant','message':{'content':[{'type':'tool_use','id':'w','name':'Write','input':{'file_path':'app/A.swift'}}]}})
        self.clock.advance(10)
        self.activity.observe(tool_end('w', tool='write'))
        self.clock.advance(120)
        self.activity.observe(tool_start('b', 'sed -n 1,5p app/A.swift'))
        self.clock.advance(5)
        self.activity.observe(tool_end('b'))
        self.clock.advance(30)
        text = self.activity.describe()
        self.assertIn('last finished: Bash: sed -n 1,5p app/A.swift (30s before the cut)', text)
        self.assertIn('last file write 2.6 min before the cut', text)
        self.assertIn('2 tool calls in this attempt', text)

    def test_a_failed_write_is_not_counted_as_a_write(self):
        self.activity.observe({'type':'assistant','message':{'content':[{'type':'tool_use','id':'w','name':'Edit','input':{'file_path':'a'}}]}})
        self.activity.observe(tool_end('w', tool='edit', error=True))
        self.assertIn('no file written', self.activity.describe())

    def test_a_role_that_never_reached_a_tool_says_so(self):
        self.assertIn('no tool call had started', self.activity.describe())

    def test_claude_code_tool_blocks_are_understood(self):
        self.activity.observe({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'c1', 'name': 'Bash', 'input': {'command': 'swift build'}}]}})
        self.clock.advance(400)
        self.assertIn('still running when cut: Bash: swift build (for 6.7 min)', self.activity.describe())
        self.activity.observe({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'c1', 'content': 'ok'}]}})
        self.activity.observe({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'c2', 'name': 'Write', 'input': {'file_path': 'a.swift'}}]}})
        self.activity.observe({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'c2'}]}})
        self.assertIn('last file write 0s before the cut', self.activity.describe())

    def test_repeated_or_malformed_events_do_not_double_count(self):
        self.activity.observe(tool_start('1', 'a'))
        self.activity.observe(tool_start('1', 'a'))
        for junk in (None, 'text', 3, {}, {'type': 'assistant', 'message': 'x'},
                     {'type': 'user', 'message': {'content': 'x'}}):
            self.activity.observe(junk)
        self.assertEqual(1, self.activity.calls)

    def test_long_commands_are_cut_to_one_short_line(self):
        self.activity.observe(tool_start('1', 'echo ' + 'x' * 500))
        label = self.activity.describe().split('still running when cut: ')[1].split(' (for')[0]
        self.assertLessEqual(len(label), 110)

    def test_headline_leads_with_the_marker_and_the_numbers(self):
        text = timeout_headline('claude', 2399.6, 2400, self.activity)
        self.assertTrue(text.startswith(f'{TIMEOUT_MARK}: claude was cut off after 2399s (limit 2400s)'))


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time
mode = os.environ.get('FAKE_CLAUDE_MODE', 'result')
def emit(event):
    print(json.dumps(event), flush=True)
emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'working'}],
      'usage': {'input_tokens': 4, 'output_tokens': 6, 'cache_read_input_tokens': 654000,
                'cache_creation_input_tokens': 245}}})
if mode == 'hang':
    emit({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': 'c1', 'name': 'Bash', 'input': {'command': 'swift test --package-path app'}}]}})
    time.sleep(60)
usage = json.loads(os.environ.get('FAKE_MODEL_USAGE', 'null'))
event = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '{}',
         'session_id': 'sid', 'total_cost_usd': 0}
if usage is not None:
    event['modelUsage'] = usage
emit(event)
'''


class ClaudeWindowAndTimeoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-agent-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.work = self.base / 'workspace'
        self.work.mkdir()
        fake = self.base / 'claude'
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o700)
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (agent_cc.INHERIT_ENV, 'KH_CC_CONTEXT_WINDOW', 'FAKE_MODEL_USAGE', 'FAKE_CLAUDE_MODE'):
            os.environ.pop(name, None)
        cc = patch.object(agent_cc, 'CC_PATH', str(fake))
        cc.start()
        self.addCleanup(cc.stop)

    def run_claude(self, model='claude-sonnet-5-5', timeout=30, usage=None, mode='result'):
        if usage is not None:
            os.environ['FAKE_MODEL_USAGE'] = json.dumps(usage)
        os.environ['FAKE_CLAUDE_MODE'] = mode
        request = CCRequest(prompt='go', system_prompt='sys', model=model, session_id='sid',
                            session_dir=str(self.base / 'sessions'),
                            raw_output_path=str(self.base / 'attempt' / 'raw_output.jsonl'),
                            tools=['read'], cwd=str(self.work), timeout_seconds=timeout)
        return agent_cc.run(request)

    def test_the_window_the_cli_reports_replaces_the_200k_assumption(self):
        result = self.run_claude(usage={'claude-sonnet-5-5': {'contextWindow': 1_000_000}})
        self.assertEqual(1_000_000, result.context_window)

    def test_without_a_report_the_old_assumption_stands(self):
        self.assertEqual(200_000, self.run_claude().context_window)
        self.assertEqual(200_000, self.run_claude(usage={'claude-sonnet-5-5': {}}).context_window)

    def test_the_operator_pin_still_wins(self):
        os.environ['KH_CC_CONTEXT_WINDOW'] = '500000'
        result = self.run_claude(usage={'claude-sonnet-5-5': {'contextWindow': 1_000_000}})
        self.assertEqual(500_000, result.context_window)

    def test_the_entry_is_chosen_by_model_not_by_position(self):
        usage = {'claude-haiku-5-5': {'contextWindow': 200_000},
                 'claude-sonnet-5-5': {'contextWindow': 1_000_000}}
        self.assertEqual(1_000_000, self.run_claude(model='claude-sonnet-5-5', usage=usage).context_window)
        self.assertEqual(1_000_000, self.run_claude(model='sonnet', usage=usage).context_window)
        self.assertEqual(200_000, self.run_claude(model='claude-haiku-5-5', usage=usage).context_window)

    def test_several_entries_and_no_match_guesses_nothing(self):
        usage = {'claude-haiku-5-5': {'contextWindow': 1_000_000},
                 'claude-sonnet-5-5': {'contextWindow': 1_000_000}}
        self.assertEqual(200_000, self.run_claude(model='opus', usage=usage).context_window)

    def test_a_dated_or_1m_suffixed_id_still_matches(self):
        self.assertEqual(1_000_000, agent_cc.reported_window(
            {'modelUsage': {'claude-sonnet-5-5-20261001': {'contextWindow': 1_000_000}}}, 'claude-sonnet-5-5'))
        self.assertEqual(1_000_000, agent_cc.reported_window(
            {'modelUsage': {'claude-sonnet-5-5[1m]': {'contextWindow': 1_000_000}}}, 'claude-sonnet-5-5'))

    def test_junk_in_the_report_is_ignored(self):
        for junk in (None, [], {'modelUsage': 'x'}, {'modelUsage': {'a': {'contextWindow': True}}},
                     {'modelUsage': {'a': {'contextWindow': -5}}}, {'modelUsage': {'a': 3}}):
            self.assertEqual(0, agent_cc.reported_window(junk, 'a'))

    def test_the_failing_reviewer_reading_is_no_longer_over_the_guard_line(self):
        """654,255 tokens: 327% of an assumed 200K, 65% of the reported 1M."""
        before = self.run_claude()
        percent, tokens, window = context_guard.occupancy(before)
        self.assertEqual((654_255, 200_000), (tokens, window))
        self.assertGreater(percent, 300)
        after = self.run_claude(usage={'claude-sonnet-5-5': {'contextWindow': 1_000_000}})
        percent, tokens, window = context_guard.occupancy(after)
        self.assertEqual((654_255, 1_000_000), (tokens, window))
        self.assertAlmostEqual(65.4, percent, places=1)
        self.assertLess(percent, 75.0)

    def test_a_claude_timeout_carries_the_headline_and_the_running_command(self):
        with self.assertRaises(RuntimeError) as caught:
            self.run_claude(timeout=2, mode='hang')
        message = str(caught.exception)
        self.assertTrue(message.startswith('claude exited 124: ' + TIMEOUT_MARK), message)
        self.assertIn('still running when cut: Bash: swift test --package-path app', message)
        self.assertIn('Timed out after', message)
        self.assertEqual('', transient.classify(caught.exception))


if __name__ == '__main__':
    unittest.main()
