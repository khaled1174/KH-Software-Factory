"""Real ledger/locking preserve exact continuous-controller state fields."""
import copy
import os
import unittest
from unittest.mock import patch
from tests import test_run_next as fixture
from tests.test_run_next import adw_project, adw_start, project


class ControllerStateContractTests(unittest.TestCase):
    setUp = fixture.RunNextTests.setUp
    tearDown = fixture.RunNextTests.tearDown
    receipt = fixture.RunNextTests.receipt
    reviewing = fixture.RunNextTests.reviewing
    complete = fixture.RunNextTests.complete
    set_plan = fixture.RunNextTests.set_plan
    executor = fixture.RunNextTests.executor

    def execute(self, executor, notify=lambda _event: None):
        self.records = []
        write = adw_project._write_control
        def observe(root, name, value):
            if name == 'controller-state.json':
                self.records.append(copy.deepcopy(value))
            return write(root, name, value)
        with patch.object(adw_project, '_write_control', side_effect=observe), \
                patch.object(adw_start, 'main', side_effect=executor):
            return adw_project.run_continuous(self.root, notify=notify)

    def assert_common(self):
        common = {'schema_version', 'controller_id', 'state', 'updated_at', 'mode', 'pid',
                  'current_task', 'completed_this_run'}
        identifiers = {record['controller_id'] for record in self.records}
        self.assertEqual(len(identifiers), 1)
        self.assertTrue(next(iter(identifiers)))
        for record in self.records:
            self.assertEqual(record['schema_version'], 1)
            self.assertEqual(record['mode'], 'full')
            self.assertEqual(record['pid'], os.getpid())
            self.assertIsInstance(record['updated_at'], str)
            extras = ({'completion_percent'} if record['state'] == 'complete' else
                      {'reason'} if record['state'] in {'stopped', 'paused', 'interrupted'} else
                      {'last_completed'} if 'last_completed' in record else set())
            self.assertEqual(set(record), common | extras)

    def test_success_state_sequence_and_fields(self):
        self.set_plan()
        result = self.execute(self.executor)
        self.assertEqual(result['status'], 'complete')
        self.assert_common()
        self.assertEqual([(r['state'], r['current_task'], r['completed_this_run']) for r in self.records],
            [('running', None, 0), ('running', 'T1', 0), ('running', None, 1),
             ('running', 'T2', 1), ('running', None, 2), ('complete', None, 2)])
        self.assertEqual(self.records[-1]['completion_percent'], 100.0)
        completed = [r['last_completed'] for r in self.records if 'last_completed' in r]
        self.assertEqual([event['task_id'] for event in completed], ['T1', 'T2'])
        self.assertEqual([event['next_task'] for event in completed], ['T2', None])

    def test_task_failure_state_preserves_blocker(self):
        self.set_plan()
        def fail(**kwargs):
            project.begin_task(self.root, 'T1', 'failed')
            project.fail_task(self.root, 'T1', 'failed', 'Verification rejected candidate')
            return 1
        result = self.execute(fail)
        self.assertEqual(result['status'], 'stopped')
        self.assert_common()
        self.assertEqual(self.records[-1]['reason'], 'Verification rejected candidate')
        self.assertEqual(self.records[-1]['completed_this_run'], 0)

    def test_notification_failure_stops_after_one_acceptance(self):
        self.set_plan()
        def notify(_event):
            raise OSError('notification unavailable')
        result = self.execute(self.executor, notify)
        self.assertEqual(result['status'], 'stopped')
        self.assert_common()
        self.assertEqual(self.records[-1]['reason'], 'Owner notification failed; no later task was dispatched')
        self.assertEqual(self.records[-1]['completed_this_run'], 1)
        self.assertEqual(project.load_task(self.root, 'T2')['status'], 'pending')

    def test_pause_state_keeps_accepted_task_and_pending_successor(self):
        self.set_plan()
        def pause(**kwargs):
            adw_project.request_pause_after_current(self.root)
            return self.executor(**kwargs)
        result = self.execute(pause)
        self.assertEqual(result['status'], 'paused')
        self.assert_common()
        self.assertEqual(self.records[-1]['reason'], 'Owner requested pause after the current task')
        self.assertEqual(self.records[-1]['completed_this_run'], 1)

    def test_interrupt_state_preserves_unfinished_task(self):
        self.set_plan()
        with self.assertRaises(KeyboardInterrupt):
            self.execute(KeyboardInterrupt())
        self.assert_common()
        self.assertEqual(self.records[-1]['state'], 'interrupted')
        self.assertEqual(self.records[-1]['reason'], 'Continuous controller was interrupted')
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'pending')

    def test_no_ready_task_stays_stopped_without_dispatch(self):
        self.set_plan()
        project.begin_task(self.root, 'T1', 'failed')
        project.fail_task(self.root, 'T1', 'failed', 'Blocked prerequisite')
        result = self.execute(self.executor)
        self.assertEqual(result['attempted'], [])
        self.assert_common()
        self.assertEqual(self.records[-1]['state'], 'stopped')
        self.assertEqual(self.records[-1]['reason'], 'No ready pending task; resolve blocked or interrupted work')

    def test_dispatch_exception_keeps_error_and_pending_task(self):
        self.set_plan()
        result = self.execute(OSError('provider unavailable'))
        self.assertEqual(result['status'], 'stopped')
        self.assert_common()
        self.assertEqual(self.records[-1]['reason'], 'provider unavailable')
        self.assertIsNone(self.records[-1]['current_task'])
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
