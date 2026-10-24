"""Guarded light CLI uses actual controller execution with local fake transport."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import importlib.util
import json
import subprocess
import sys
import unittest
from unittest.mock import patch
from tests import test_start

ADWS = test_start.ADWS


class LightModeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_start.StartupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root

    def invoke(self, mode='good', extra=()):
        return subprocess.run([sys.executable, str(ADWS/'adw_simple_sdlc.py'),
            '--scope-file', str(self.fixture.scope), '--adw-id', 'light', *extra,
            '--', 'Implement value 2'], cwd=self.root,
            env={**self.fixture.env, 'KH_FIXTURE_MODE': mode},
            text=True, capture_output=True, timeout=60)

    def budget(self):
        return json.loads((self.root/'adws/adw_data/sessions/light/execution_budget-task.json').read_text())

    def test_success_four_task_sends_preserves_independent_review(self):
        result = self.invoke()
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertEqual('value = 2\n', (self.root/'src/app.py').read_text())
        budget = self.budget()
        self.assertEqual(4, budget['model_sends'])
        self.assertEqual(0, budget['code_repairs'])
        self.assertTrue((self.root/'adws/adw_data/sessions/light/execution_budget-bootstrap.json').is_file())

    def test_failed_checks_have_at_most_one_shared_repair(self):
        self.fixture.cfg['defaults']['quality_checks']['test']=[sys.executable,'-c','raise SystemExit(7)']
        self.fixture.config.write_text(json.dumps(self.fixture.cfg))
        result=self.invoke()
        self.assertNotEqual(0,result.returncode)
        self.fixture.unchanged()
        self.assertEqual(1,self.budget()['code_repairs'])
        self.assertEqual(4,self.budget()['model_sends'])

    def test_one_repair_can_be_accepted_after_real_recheck(self):
        result=self.invoke('test-repair')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(1,self.budget()['code_repairs'])
        self.assertEqual(5,self.budget()['model_sends'])

    def test_retired_panel_is_ignored_with_one_independent_combined_review(self):
        self.fixture.cfg['review']={'panel':['scope','scope','unknown-legacy-lens']}
        self.fixture.config.write_text(json.dumps(self.fixture.cfg))
        result=self.invoke()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual('value = 2\n', (self.root/'src/app.py').read_text())
        self.assertEqual(4,self.budget()['model_sends'])
        run=self.root/'adws/adw_data/sessions/light'
        mapping=json.loads((run/'agent_map.json').read_text())
        self.assertIn('reviewer',mapping)
        self.assertTrue(mapping['reviewer']['phase_id'].endswith('_review_1'))
        self.assertFalse(list(run.rglob('*review_1_scope*')))

    def test_plan_reviewer_can_reject_light_eligibility(self):
        self.fixture.cli.write_text(test_start.FAKE.replace('light_eligible=True','light_eligible=False'))
        result=self.invoke()
        self.assertNotEqual(0,result.returncode)
        self.fixture.unchanged()
        mapping=json.loads((self.root/'adws/adw_data/sessions/light/agent_map.json').read_text())
        self.assertNotIn('builder',mapping)
        self.assertIn('REQUIRES_FULL_MODE',result.stdout+result.stderr)

    def test_owner_risk_path_rule_prevents_builder_dispatch(self):
        self.fixture.cfg['execution']={'light_risk_paths':['src/app.py']}
        self.fixture.config.write_text(json.dumps(self.fixture.cfg))
        result=self.invoke()
        self.assertNotEqual(0,result.returncode)
        self.fixture.unchanged()
        self.assertIn('REQUIRES_FULL_MODE',result.stdout+result.stderr)
        run=self.root/'adws/adw_data/sessions/light/agent_map.json'
        if run.exists():
            self.assertNotIn('builder',json.loads(run.read_text()))

    def test_dry_run_has_no_source_or_session_writes(self):
        result=self.invoke(extra=('--dry-run',))
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.fixture.unchanged()
        self.assertFalse((self.root/'adws/adw_data/sessions').exists())

    def test_removed_leading_legacy_option_is_rejected(self):
        result=subprocess.run([sys.executable,str(ADWS/'adw_simple_sdlc.py'),
                              '--legacy','request'],cwd=self.root,
                              env=self.fixture.env,text=True,capture_output=True)
        self.assertNotEqual(0,result.returncode)
        self.assertIn('unrecognized arguments',result.stderr)
        self.assertIn('--legacy',result.stderr)
        self.fixture.unchanged()

    def test_legacy_marker_after_separator_is_literal_task(self):
        spec=importlib.util.spec_from_file_location('light_wrapper',ADWS/'adw_simple_sdlc.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module,'main',return_value=0) as call:
            self.assertEqual(0,module.cli(['--','--legacy']))
        self.assertEqual('--legacy',call.call_args.kwargs['prompt'])


if __name__=='__main__':
    unittest.main()
