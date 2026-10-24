"""Offline corpus ground truth and faithful real-gate evaluation contracts."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evaluate_documentation as evaluation


def load_file(path):
    spec = importlib.util.spec_from_file_location('offline_ground_truth', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DocumentationEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = evaluation.read_corpus(evaluation.CORPUS)
        # Keep real repository inspection while forbidding provider dispatch
        # or arbitrary commands. No gate or permission checker is replaced.
        real_popen = subprocess.Popen
        def only_inventory(argv, *args, **kwargs):
            if argv not in (['git', 'ls-files', '-z'], ['git', 'rev-parse', '--absolute-git-dir']):
                raise AssertionError('Offline evaluator dispatched an unexpected command: ' + str(argv))
            return real_popen(argv, *args, **kwargs)
        with patch('subprocess.Popen', side_effect=only_inventory):
            cls.result = evaluation.evaluate()

    def test_real_gates_and_controls_execute_without_providers(self):
        result = self.result
        self.assertEqual(result['counts'], {'defective_guides': 7, 'correct_guides': 6, 'integrity_controls': 6})
        self.assertEqual(result['provider_calls'], 0)
        self.assertEqual(set(result['metrics']), {'artifacts_exist', 'files_non_empty', 'claims_gate',
                                                'proposal_gate', 'project_context_readiness', 'combined'})
        for control in result['integrity_controls']:
            self.assertFalse(control['gates'][control['target_gate']]['passed'], control['id'])
        controls = {control['id']: control for control in result['integrity_controls']}
        self.assertNotIn('missing_coverage', controls)  # Coverage is measured by the controller.
        self.assertIn('app_docs/unapproved.md',
            str(controls['unapproved_document']['gates']['proposal_gate']))
        for row in result['cases']:
            self.assertEqual(row['provider_calls'], 0)
            self.assertEqual(row['initial_context_status'], 'ready')
        self.assertNotIn('kh-doc-eval-', json.dumps(result))

    def test_labels_are_only_scoring_inputs(self):
        rows = self.result['cases']
        before = json.dumps([row['gates'] for row in rows], sort_keys=True)
        inverted = [{**row, 'defective': not row['defective']} for row in rows]
        for gate, original in self.result['metrics'].items():
            flipped = evaluation.score(inverted, gate)
            self.assertEqual(flipped['caught'], original['false_alarm'])
            self.assertEqual(flipped['missed'], original['correct_accept'])
            self.assertEqual(flipped['false_alarm'], original['caught'])
            self.assertEqual(flipped['correct_accept'], original['missed'])
        self.assertEqual(before, json.dumps([row['gates'] for row in inverted], sort_keys=True))

    def test_source_freshness_detects_both_old_feature_descriptions_without_false_alarms(self):
        detected = {row['id'] for row in self.result['cases']
                    if not row['gates']['project_context_readiness']['passed']}
        self.assertEqual(detected, {'bad-feature-state', 'bad-parallel-feature-state'})
        self.assertEqual(self.result['metrics']['project_context_readiness'],
            {'caught': 2, 'missed': 5, 'false_alarm': 0, 'correct_accept': 6})

    def test_stale_fixture_changes_real_sources_only_after_review(self):
        row = next(row for row in self.result['cases'] if row['id'] == 'bad-feature-state')
        self.assertEqual(set(row['changed_paths']), set(self.manifest['before_hashes']))
        for name in row['changed_paths']:
            self.assertNotEqual(row['source_before'][name], row['source_after'][name])
        case = next(c for c in self.manifest['cases'] if c['id'] == row['id'])
        self.assertEqual(row['guide_sha256'], case['guide_sha256'])

    def test_feature_state_snapshots_match_the_physical_review_and_changed_source(self):
        row = next(row for row in self.result['cases'] if row['id'] == 'bad-feature-state')
        for name, after in self.manifest['base_hashes'].items():
            before = self.manifest['before_hashes'].get(name, after)
            self.assertEqual(row['source_before'][name], before, name)
            self.assertEqual(row['source_after'][name], after, name)
        for name in self.manifest['before_hashes']:
            original = evaluation.CORPUS / self.manifest['before'] / name
            changed = evaluation.CORPUS / self.manifest['base'] / name
            self.assertEqual(evaluation.sha(original.read_bytes()), row['source_before'][name])
            self.assertEqual(evaluation.sha(changed.read_bytes()), row['source_after'][name])
            self.assertNotEqual(original.read_bytes(), changed.read_bytes())

    def test_parallel_ticket_a_changes_a_dependency_of_retained_ticket_b_guide(self):
        row = next(row for row in self.result['cases'] if row['id'] == 'bad-parallel-feature-state')
        dependency = 'logreports/reports/status_counts.py'
        self.assertEqual(row['changed_paths'], [dependency])
        trace = row['parallel_merge_order']
        self.assertEqual(trace['events'], ['B_reviewed', 'A_and_B_forked', 'A_changed', 'A_merged',
                                           'before_B_acceptance'])
        self.assertEqual(trace['ticket_a_source_at_fork'], row['source_before'])
        self.assertEqual(trace['ticket_b_source_at_fork'], row['source_before'])
        self.assertEqual(trace['ticket_a_source_after_change'], row['source_after'])
        self.assertEqual(trace['ticket_b_source_after_a_merge'], row['source_before'])
        self.assertEqual(trace['ticket_b_document_at_fork'], row['guide_sha256'])
        self.assertEqual(trace['ticket_b_document_after_a_merge'], row['guide_sha256'])
        self.assertEqual(trace['main_document_before_b_acceptance'], row['guide_sha256'])
        self.assertNotEqual(trace['ticket_b_source_after_a_merge'][dependency], row['source_after'][dependency])
        guide = next(c['guide'] for c in self.manifest['cases'] if c['id'] == row['id'])
        self.assertIn(dependency, evaluation.fixture_file(evaluation.CORPUS, guide).read_text())

    def test_distinct_guides_and_five_families_plus_build_omission(self):
        cases = self.manifest['cases']
        self.assertEqual(len({c['guide_sha256'] for c in cases}), 13)
        self.assertGreaterEqual(sum(not c['defective'] for c in cases), 3)
        self.assertEqual({c['category'] for c in cases}, {'feature_state', 'exception_taxonomy',
            'parser_position', 'unsupported_restriction', 'build_absent', 'build_omitted'})
        omitted = next(c for c in cases if c['id'] == 'bad-build-omitted')
        self.assertEqual(omitted['defect_kind'], 'coverage')
        for copied in self.manifest['source_fixtures']:
            self.assertEqual(evaluation.sha(evaluation.fixture_file(evaluation.CORPUS, copied['fixture']).read_bytes()),
                             copied['sha256'])

    def test_ground_truth_loader_and_parser_counterexamples(self):
        core = load_file(evaluation.CORPUS / 'base/logreports/core.py')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.jsonl'
            for value, expected in [({'service': 'api', 'status': 200, 'duration_ms': 1}, KeyError),
                                    ([], AttributeError), ({'service': ''}, ValueError)]:
                path.write_text(json.dumps(value) + '\n')
                with self.assertRaises(expected):
                    core.load_records(path)
            path.write_text('{\n')
            with self.assertRaises(json.JSONDecodeError) as raised:
                core.load_records(path)
            self.assertEqual((raised.exception.lineno, raised.exception.colno), (2, 1))
            self.assertIsInstance(raised.exception, ValueError)
        probe = load_file(evaluation.CORPUS / 'base/probes/parser_position.py')
        self.assertEqual(probe.probe(), {'lineno': 2, 'colno': 1, 'is_value_error': True})

    def test_ground_truth_reports_are_implemented_and_have_tests(self):
        records = [{'service': 'api', 'status': 503, 'duration_ms': 20,
                    'timestamp': '2026-10-01T00:00:00+00:00'}]
        expected = {'status_counts': {'2xx': 0, '3xx': 0, '4xx': 0, '5xx': 1},
                    'latency': {'count': 1, 'min_ms': 20, 'max_ms': 20, 'mean_ms': 20, 'p95_ms': 20},
                    'failures': {'api': 1}, 'daily': {'2026-10-01': 1}}
        for name, value in expected.items():
            report = load_file(evaluation.CORPUS / f'base/logreports/reports/{name}.py')
            self.assertEqual(report.summarize(records), value)
            self.assertTrue((evaluation.CORPUS / f'base/tests/test_{name}.py').is_file())
        owner = (evaluation.CORPUS / 'base/OWNER_RULES.md').read_text()
        self.assertIn('dependencies are permitted', owner)
        config = json.loads((evaluation.CORPUS / 'base/adws/adw_kh_config/kh.config.yaml').read_text())
        self.assertIn('build', config['defaults']['quality_checks'])
        self.assertIn('\nbuild:\n', (evaluation.CORPUS / 'base/Makefile').read_text())

    def test_modified_fixture_is_setup_failure_not_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            copied = Path(folder) / 'corpus'
            shutil.copytree(evaluation.CORPUS, copied)
            (copied / self.manifest['cases'][0]['guide']).write_text('Changed evidence\n')
            with self.assertRaisesRegex(ValueError, 'guide hash mismatch'):
                evaluation.evaluate(copied)

    def test_unsafe_paths_and_nonboolean_labels_are_rejected(self):
        for value in ('../outside', '/outside', 'a/../outside', './file', 'a\\file', '.', 'a\nfile'):
            with self.assertRaises(ValueError):
                evaluation.relative(value)
        with tempfile.TemporaryDirectory() as folder:
            copied = Path(folder) / 'corpus'
            shutil.copytree(evaluation.CORPUS, copied)
            manifest = json.loads((copied / 'corpus.json').read_text())
            manifest['cases'][0]['defective'] = 'false'
            (copied / 'corpus.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'label must be boolean'):
                evaluation.read_corpus(copied)
            for updates, message in [({'parallel_source_change': 'true'}, 'parallel flag'),
                                      ({'source_change_paths': ['../outside']}, 'source changes'),
                                      ({'parallel_source_change': True, 'source_changed_after_review': False}, 'changed source snapshot')]:
                manifest = json.loads((evaluation.CORPUS / 'corpus.json').read_text())
                manifest['cases'][0].update(updates)
                (copied / 'corpus.json').write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, message):
                    evaluation.read_corpus(copied)
            manifest = json.loads((evaluation.CORPUS / 'corpus.json').read_text())
            manifest['schema_version'] = 1
            (copied / 'corpus.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'require corpus schema 2'):
                evaluation.read_corpus(copied)
            manifest['cases'] = [c for c in manifest['cases'] if not c.get('parallel_source_change')]
            (copied / 'corpus.json').write_text(json.dumps(manifest))
            self.assertEqual(len(evaluation.read_corpus(copied)['cases']), 12)

    def test_cli_reproduces_results_and_refuses_overwrite(self):
        command = [sys.executable, '-B', str(ROOT / 'scripts/evaluate_documentation.py')]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'baseline.json'
            tools = Path(folder) / 'tools'
            tools.mkdir()
            (tools / 'git').symlink_to(shutil.which('git'))
            env = {**os.environ, 'PATH': str(tools), 'CLAUDE_CODE_PATH': '/no-native-claude'}
            run = subprocess.run([*command, '--output', str(output)], capture_output=True, text=True, timeout=90, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(output.read_text()), self.result)
            original = output.read_bytes()
            repeat = subprocess.run([*command, '--output', str(output)], capture_output=True, text=True, timeout=90, env=env)
            self.assertEqual(repeat.returncode, 1)
            self.assertIn('Cannot create evaluation output', repeat.stderr)
            self.assertEqual(output.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
