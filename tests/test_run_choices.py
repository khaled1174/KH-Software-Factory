"""The owner answers "which model, which effort?" per run; nothing is persisted."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import test_start
sys.path.insert(0, str(test_start.ADWS))
from adw_modules import agents, project, routing  # noqa: E402


class RunChoiceTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_start.StartupTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.root = self.fx.root
        self.run_dir = self.root / 'adws/adw_data/sessions/guarded'

    def cfg(self):
        return agents.load_config(str(self.fx.config))

    # Validation: same rules as /kh-setup-models, for one run.

    def test_choices_are_validated_like_setup_and_name_every_problem(self):
        choices = routing.run_choices(self.cfg(), ['builder=claude_code:owner-builder:high',
                                                   'reviewer=delegate_codex:gpt-5.6:xhigh'])
        self.assertEqual(choices['builder'].model_dump(exclude_unset=True),
                         {'coding_agent': 'claude_code', 'model': 'owner-builder', 'thinking': 'high',
                          'supported_thinking': ['high']})
        self.assertIn('tools', choices['reviewer'].model_fields_set)
        self.assertIsNone(choices['reviewer'].tools)
        with self.assertRaises(ValueError) as caught:
            routing.run_choices(self.cfg(), ['scout=claude_code:haiku:low', 'builder=vertex:x:high',
                                             'planner=delegate_codex:gpt:max', 'reviewer=opus',
                                             'builder=claude_code:opus:high', 'builder=claude_code:sonnet:high'])
        message = str(caught.exception)
        for expected in ("'scout': not a routing role", "unknown runtime 'vertex'",
                         "effort 'max' not supported by delegate_codex", "expected ROLE=RUNTIME:MODEL:EFFORT",
                         'builder: chosen twice'):
            self.assertIn(expected, message)
        self.assertEqual(routing.run_choices(self.cfg(), None), {})

    def test_override_replaces_only_the_answered_roles_and_is_recorded(self):
        choices = routing.run_choices(self.cfg(), ['reviewer=claude_code:opus:xhigh'])
        resolved, decision = routing.resolve(self.cfg(), self.root, available=lambda a: True,
                                             required_roles=routing.REVIEWED_ROLES, overrides=choices)
        by_role = {a.name: (a.coding_agent, a.model, a.thinking) for a in resolved.agents}
        self.assertEqual(by_role['reviewer'], ('claude_code', 'opus', 'xhigh'))
        self.assertEqual(by_role['builder'], ('claude_code', 'sonnet', 'high'))
        self.assertEqual(decision['run_choices']['reviewer']['model'], 'opus')
        reviewer = next(d for d in decision['roles'] if d['role'] == 'reviewer')
        self.assertEqual((reviewer['requested']['model'], reviewer['requested']['thinking']), ('opus', 'xhigh'))
        _, plain = routing.resolve(self.cfg(), self.root, available=lambda a: True,
                                   required_roles=routing.REVIEWED_ROLES)
        self.assertEqual(plain['run_choices'], {})



    def test_menu_offers_current_first_then_configured_choices_only(self):
        self.fx.cfg['routing']['profiles']['alternate']['roles']['builder'] = [
            {'coding_agent': 'delegate_codex', 'model': 'owner-codex', 'thinking': 'high',
             'supported_thinking': ['high'], 'tools': None}]
        self.fx.config.write_text(json.dumps(self.fx.cfg))
        menu = routing.choice_menu(self.cfg(), self.root, roles=routing.REVIEWED_ROLES | {'project_architect'})
        builder = menu['roles']['builder']
        self.assertEqual(builder['current'], {'coding_agent': 'claude_code', 'model': 'sonnet', 'thinking': 'high'})
        self.assertEqual(builder['options'][0], builder['current'])
        self.assertIn({'coding_agent': 'delegate_codex', 'model': 'owner-codex', 'thinking': 'high'},
                      builder['options'])
        self.assertLessEqual(len(builder['options']), 4)
        self.assertEqual(len({json.dumps(o, sort_keys=True) for o in builder['options']}), len(builder['options']))
        offered = {o['model'] for role in menu['roles'].values() for o in role['options']}
        self.assertLessEqual(offered, {'sonnet', 'owner-codex'}, 'no invented model ids')
        self.assertEqual(set(menu['runtimes']), {'claude_code', 'delegate_codex'})

    # Through the real controller.

    def test_start_runs_with_the_answered_model_and_persists_nothing(self):
        result = self.fx.invoke(extra=['--use', 'reviewer=claude_code:opus:xhigh'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mapping = json.loads((self.run_dir / 'agent_map.json').read_text())
        self.assertEqual(mapping['reviewer']['model'], 'opus')
        self.assertEqual(mapping['builder']['model'], 'sonnet')
        decision = json.loads((self.run_dir / 'routing.json').read_text())
        self.assertEqual(decision['run_choices']['reviewer']['thinking'], 'xhigh')
        self.assertFalse((self.root / '.kh/models.json').exists())
        self.assertFalse((self.root / '.kh/routing.json').exists())

    def test_a_bad_answer_fails_before_any_dispatch(self):
        result = self.fx.invoke(extra=['--use', 'builder=claude_code:opus:extreme'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("effort 'extreme' not supported by claude_code", result.stderr)
        self.assertFalse((self.root / 'adws/adw_data/sessions').exists())
        self.fx.unchanged()

    def test_per_run_choices_cannot_be_saved_as_a_profile(self):
        result = self.fx.invoke(extra=['--use', 'reviewer=claude_code:opus:high', '--save-profile'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('/kh-setup-models --set', result.stderr)
        self.fx.unchanged()

    def test_choices_listing_is_read_only(self):
        result = subprocess.run([sys.executable, str(test_start.ADWS / 'adw_models.py'), '--choices',
                                 '--config', str(self.fx.config)], cwd=self.root, env=self.fx.env,
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        menu = json.loads(result.stdout)
        self.assertEqual(sorted(menu['roles']), ['builder', 'plan_reviewer', 'planner', 'project_architect',
                                                 'reviewer'], 'context is missing, so the architect runs too')
        self.assertEqual(menu['roles']['plan_reviewer']['current']['model'], 'sonnet')
        self.assertFalse((self.root / 'adws/adw_data').exists())
        self.assertFalse((self.root / '.kh').exists())

    # Batches: one answer for every task.

    def test_batch_passes_the_answer_to_every_task_and_refuses_a_bad_one_first(self):
        import adw_project
        from tests import test_corrective_release as corrective
        corrective.CorrectiveReleaseTests.init_plan(self.fx)
        seen = []
        with patch('adw_start.main', side_effect=lambda **kw: seen.append(kw['use']) or 1):
            adw_project.run_continuous(self.root, config=str(self.fx.config), use=['reviewer=claude_code:opus:high'])
        self.assertEqual(seen, [['reviewer=claude_code:opus:high']])
        with patch('adw_start.main') as start, self.assertRaises(ValueError):
            adw_project.run_continuous(self.root, config=str(self.fx.config), use=['reviewer=claude_code:opus'])
        start.assert_not_called()
        with patch('adw_start.main') as start, self.assertRaises(ValueError):
            adw_project.run_continuous(self.root, config=str(self.fx.config), use=['nobody=pi:x:high'])
        start.assert_not_called()
        self.assertEqual(project.load_task(self.root, 'T2')['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
