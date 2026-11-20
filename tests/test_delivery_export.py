"""Delivery packet reads matching committed receipts and explicitly selected evidence."""
import hashlib
import json
import sys
import unittest
from urllib.parse import urlencode

from tests import test_corrective_release as corrective
from tests import test_start as startup
from tests import test_visualizer_api as visualizer

sys.path.insert(0, str(startup.ADWS))
from adw_modules import project


@unittest.skipUnless(visualizer.BUN, 'Bun is unavailable; set KH_BUN to test delivery exports')
class DeliveryExportTests(unittest.TestCase):
    server = visualizer.VisualizerApiTests.server
    request = visualizer.VisualizerApiTests.request
    init_plan = corrective.CorrectiveReleaseTests.init_plan
    tracked = corrective.CorrectiveReleaseTests.tracked

    def setUp(self):
        startup.StartupTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.db_path = self.root / 'adws/adw_data/kh.db'
        self.init_plan()

    def delivery(self, base, run_id, *selected, task_id='T1'):
        query = urlencode({'run_id': run_id, 'artifact': selected}, doseq=True)
        return self.request(base, f'/api/project/tasks/{task_id}/delivery?{query}')

    def test_accepted_packet_binds_receipt_and_omits_unselected_logs(self):
        capture = 'PRIVATE_FIXTURE_CAPTURE'
        self.cfg['defaults']['quality_checks']['test'][-1] += f"; print('{capture}')"
        self.config.write_text(json.dumps(self.cfg))
        result = self.tracked(run='delivery-good')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt_file = project.receipt_path(self.root, 'T1', 'delivery-good')
        receipt = json.loads(receipt_file.read_text())
        handoff = self.db_path.parent / 'sessions/delivery-good/context_handoff'
        manifest_file = handoff.parent / 'artifact-map.json'
        with self.server(root=self.root) as base:
            code, packet = self.delivery(base, 'delivery-good')
            self.assertEqual(code, 200, packet)
            self.assertTrue(packet['accepted'])
            self.assertEqual(packet['filename'], 'kh-T1-delivery-good-delivery.md')
            self.assertIn(receipt['source_sha256'], packet['markdown'])
            self.assertIn(hashlib.sha256(receipt_file.read_bytes()).hexdigest(), packet['markdown'])
            self.assertIn('test \u00b7 Passed', packet['markdown'])
            self.assertIn('+value = 2', packet['markdown'])
            self.assertNotIn(capture, packet['markdown'])
            log = next(option for option in packet['artifacts'] if option['name'].endswith('/command.log'))
            code, selected = self.delivery(base, 'delivery-good', log['id'])
            self.assertEqual(code, 200, selected)
            self.assertIn(capture, selected['markdown'])

            outside = self.root / 'private.txt'
            outside.write_text('OUTSIDE THE PUBLISHED EVIDENCE')
            code, refused = self.delivery(base, 'delivery-good', 'a' * 64)
            self.assertEqual(code, 400, refused)
            self.assertNotIn(outside.read_text(), json.dumps(refused))
            self.assertEqual(self.delivery(base, 'delivery-good', task_id='T2')[0], 404)

            log_path = handoff / log['name']
            recorded_map = manifest_file.read_text()
            manifest = json.loads(recorded_map)
            mapping = next(row for row in manifest['mappings'] if log['name'] in row['published_handoff_files'])
            expected_sha = mapping.pop('published_handoff_sha256')[log['name']]
            self.assertEqual(expected_sha, hashlib.sha256(log_path.read_bytes()).hexdigest())
            self.assertIn(f'Artifact SHA-256 `{expected_sha}`', selected['markdown'])
            manifest_file.write_text(json.dumps(manifest))
            code, legacy = self.delivery(base, 'delivery-good', log['id'])
            self.assertEqual(code, 200, legacy)
            self.assertIn('Content integrity is unrecorded', legacy['markdown'])
            self.assertIn(capture, legacy['markdown'])

            manifest_file.write_text(recorded_map)
            log_path.write_text('REPLACED_ARTIFACT')
            code, changed = self.delivery(base, 'delivery-good', log['id'])
            self.assertEqual(code, 200, changed)
            self.assertIn('Published evidence differs from its recorded digest', changed['markdown'])
            self.assertNotIn('REPLACED_ARTIFACT', changed['markdown'])

            log_path.unlink()
            code, missing = self.delivery(base, 'delivery-good', log['id'])
            self.assertEqual(code, 200, missing)
            self.assertIn('Published evidence is missing', missing['markdown'])

            receipt_file.write_text(json.dumps({**receipt, 'source_sha256': 'b' * 64}))
            code, replaced = self.delivery(base, 'delivery-good')
            self.assertEqual(code, 200, replaced)
            self.assertFalse(replaced['accepted'])
            self.assertIn('completion receipt is missing or differs', replaced['markdown'])

    def test_failed_check_packet_is_incomplete_and_has_no_substituted_diff(self):
        self.cfg['defaults']['quality_checks']['test'] = [sys.executable, '-c', 'raise SystemExit(9)']
        self.cfg['execution'] = {'recovery_rounds': 0}
        self.cfg['routing']['profiles']['current']['max_repairs'] = 0
        self.config.write_text(json.dumps(self.cfg))
        result = self.tracked(run='delivery-stopped')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        run = project.load_task(self.root, 'T1')
        self.assertEqual(run['status'], 'blocked')
        with self.server(root=self.root) as base:
            status, packet = self.delivery(base, 'delivery-stopped')
            self.assertEqual(status, 200, packet)
            self.assertFalse(packet['accepted'])
            self.assertIn('**Incomplete.', packet['markdown'])
            self.assertIn('test \u00b7 Failed \u00b7 Exit 9', packet['markdown'])
            self.assertIn('This attempt has no available published diff', packet['markdown'])
            self.assertNotIn('Verified source SHA-256', packet['markdown'])

    def test_legacy_receipt_is_readable_without_a_trace_or_artifact_digest(self):
        run_id = 'delivery-legacy'
        project.begin_task(self.root, 'T1', run_id)
        project.transition(self.root, 'T1', run_id, 'verifying')
        project.transition(self.root, 'T1', run_id, 'reviewing')
        evidence = self.root / 'checks.txt'
        evidence.write_text('legacy check passed\n')
        receipt = {'schema_version': 1, 'task_id': 'T1', 'run_id': run_id,
                   'promoted': True, 'checks_passed': True, 'review_approved': True,
                   'source_sha256': 'c' * 64, 'summary': 'Verified legacy result',
                   'changed_files': ['src/app.py'], 'evidence': ['checks.txt']}
        file = project.receipt_path(self.root, 'T1', run_id)
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps(receipt))
        project.complete_task(self.root, 'T1', run_id, receipt)
        with self.server(root=self.root) as base:
            status, packet = self.delivery(base, run_id)
            self.assertEqual(status, 200, packet)
            self.assertTrue(packet['accepted'])
            self.assertIn(receipt['source_sha256'], packet['markdown'])
            self.assertIn('This attempt has no available published diff', packet['markdown'])
            self.assertEqual(packet['artifacts'], [])
