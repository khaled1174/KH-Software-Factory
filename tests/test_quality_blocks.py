"""Real quality commands retain their metadata, deadlines and failure evidence."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import execution_budget, quality
from adw_modules.data_types import AgentConfig, ConfigDefaults, KHConfig, ObservabilityConfig, PhaseParams
from adw_modules.runner import Run
from adw_modules.tracer import Tracer


class QualityBlockContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-quality-blocks-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.cfg = KHConfig(defaults=ConfigDefaults(data_dir='adws/adw_data'),
                            observability=ObservabilityConfig(db='adws/adw_data/kh.db'))
        self.tracer = Tracer(self.cfg.observability.db, 'adws/adw_data/sessions/blocks/events.jsonl')
        self.addCleanup(self.tracer.conn.close)
        self.run = Run(self.cfg, 'blocks', self.tracer, 'quality-block-contract')
        self.tracer.session_start('blocks', 'quality-block-contract')

    def execute(self, name, code):
        if code is not None:
            self.cfg.defaults.quality_checks[name] = [sys.executable, '-c', code]
        requested = []
        self.tool_events = []
        remaining = execution_budget.remaining_timeout
        event = self.tracer.event
        def observe_event(record):
            if record.type == 'tool_call':
                self.tool_events.append(record)
            return event(record)
        def observe_deadline(run, maximum):
            requested.append(maximum)
            return remaining(run, maximum)
        with self.run.phase(PhaseParams(name=name, kind='code', owner='quality', description='Real quality block')):
            with patch.object(execution_budget, 'remaining_timeout', side_effect=observe_deadline), \
                    patch.object(self.tracer, 'event', side_effect=observe_event):
                result = getattr(quality, name)(self.run)
        return result, requested

    def test_each_block_runs_its_configured_command_with_original_metadata_and_deadline(self):
        for name, operation, maximum in [('test', 'build', 600), ('lint', 'lint', 120),
                                         ('typecheck', 'typecheck', 120), ('build', 'build', 120)]:
            with self.subTest(block=name):
                result, requested = self.execute(name, 'print("ACTUAL-' + name + '")')
                self.assertTrue(result.passed, result)
                self.assertEqual((result.name, result.area, result.operation), (name, 'backend', operation))
                self.assertEqual(requested, [maximum])
                self.assertEqual(result.returncode, 0)
                log = Path(result.output_artifact).read_text()
                self.assertIn('ACTUAL-' + name, log.split('--- stdout ---', 1)[1])
                self.assertEqual(len(self.tool_events), 1)
                event = self.tool_events[0]
                self.assertEqual(event.name, 'quality:' + name)
                self.assertEqual(set(event.payload), {'area', 'operation', 'command', 'returncode', 'passed',
                    'output_artifact', 'streams_complete', 'cleanup'})
                self.assertEqual(event.payload['area'], 'backend')
                self.assertEqual(event.payload['operation'], operation)
                self.assertEqual(event.payload['returncode'], 0)
                self.assertTrue(event.payload['passed'])
                self.assertEqual(event.payload['command'], result.command)
                self.assertEqual(event.payload['output_artifact'], result.output_artifact)

    def test_each_block_preserves_failed_exit_and_output(self):
        for name in ['test', 'lint', 'typecheck', 'build']:
            with self.subTest(block=name):
                result, _ = self.execute(name, 'import sys; print("FAILURE-' + name + '"); sys.exit(7)')
                self.assertFalse(result.passed)
                self.assertEqual(result.returncode, 7)
                self.assertIn('FAILURE-' + name, result.output_tail)

    def test_each_missing_command_fails_closed_without_starting_work(self):
        for name in ['test', 'lint', 'typecheck', 'build']:
            with self.subTest(block=name):
                result, requested = self.execute(name, None)
                self.assertFalse(result.passed)
                self.assertEqual(result.returncode, 126)
                self.assertIn('NOT_CONFIGURED', result.output_tail)
                self.assertEqual(requested, [])

    def test_owned_output_remains_excluded_from_both_fingerprints(self):
        self.cfg.agents.append(AgentConfig(name='builder', model='offline', writes=['src/**'],
            prompt_engineering={'system': 'unused', 'user': 'unused'}))
        (self.root / 'src').mkdir()
        (self.root / 'src/main.py').write_text('value = 1\n')
        (self.root / 'evidence').mkdir()
        output = self.root / 'evidence/result.txt'
        output.write_text('before\n')
        self.run.agent_output_relative = 'evidence'
        before = quality.source_fingerprint(self.run), quality.workable_fingerprint(self.run)
        output.write_text('after\n')
        self.assertEqual((quality.source_fingerprint(self.run), quality.workable_fingerprint(self.run)), before)
        (self.root / 'src/main.py').write_text('value = 2\n')
        self.assertNotEqual(quality.source_fingerprint(self.run), before[0])
        self.assertNotEqual(quality.workable_fingerprint(self.run), before[1])

    def test_non_builder_source_affects_source_but_not_workable_fingerprint(self):
        self.cfg.agents.append(AgentConfig(name='builder', model='offline', writes=['src/**'],
            prompt_engineering={'system': 'unused', 'user': 'unused'}))
        (self.root / 'src').mkdir()
        (self.root / 'src/main.py').write_text('value = 1\n')
        outside = self.root / 'outside.py'
        outside.write_text('value = 1\n')
        before = quality.source_fingerprint(self.run), quality.workable_fingerprint(self.run)
        outside.write_text('value = 2\n')
        self.assertNotEqual(quality.source_fingerprint(self.run), before[0])
        self.assertEqual(quality.workable_fingerprint(self.run), before[1])

    def test_aggregate_still_runs_every_failing_block_in_order(self):
        names = ['test', 'lint', 'typecheck', 'build']
        for index, name in enumerate(names):
            self.cfg.defaults.quality_checks[name] = [sys.executable, '-c',
                'import sys; print("FAILED-' + name + '"); sys.exit(' + str(7 + index) + ')']
        with self.run.phase(PhaseParams(name='all', kind='code', owner='quality', description='Aggregate failures')):
            result = quality.run_quality(self.run)
        self.assertFalse(result.passed)
        self.assertEqual([check.name for check in result.checks], names)
        self.assertEqual([check.returncode for check in result.checks], [7, 8, 9, 10])
        envelope = quality.as_envelope(result, 'Aggregate')
        self.assertEqual(envelope.artifacts, result.artifacts)
        self.assertEqual(envelope.failures, result.failures)
        self.assertFalse(envelope.passed)
        self.assertEqual(envelope.status, 'fail')


if __name__ == '__main__':
    unittest.main()
