"""Read one actual guarded success; distinct unscoped success remains independent."""
import json
from pathlib import Path
import unittest
from tests import test_start as startup


class SharedHappyPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = startup.StartupTests()
        cls.fixture.setUp()
        cls.addClassCleanup(cls.fixture.tearDown)
        cls.root = cls.fixture.root
        cls.run_dir = cls.root / 'adws/adw_data/sessions/guarded'
        cls.result = cls.fixture.invoke()

    def test_success_real_subprocess_promotes_after_review(self):
        result = self.result
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')
        run=self.root/'adws/adw_data/sessions/guarded'
        accepted=json.loads((run/'acceptance.json').read_text())
        self.assertIn('Implement value 2',accepted)
        self.assertIn('Out of scope: Unrelated features',accepted)
        self.assertIn('The scoped behavior returns value 2',accepted)
        mapping=json.loads((run/'agent_map.json').read_text())
        self.assertNotEqual(mapping['builder']['session_id'],mapping['reviewer']['session_id'])
        self.assertFalse((self.root/'.kh/routing.json').exists())
        self.assertFalse((self.root/'.kh/model-switch').exists())
        self.assertTrue(list(run.rglob('command.log')))
        for envelope in run.glob('*/phases/*/envelope-*.json'):
            for path in json.loads(envelope.read_text()).get('artifacts',[]):
                self.assertTrue(Path(path).is_file(),path)


    def test_ordinary_run_has_no_floor_requirement(self):
        result = self.result
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.run_dir / 'context_handoff/floor_guard_1.json').read_text()), [])
        prompt = next(self.run_dir.glob('reviewer/phases/*_review_1/prompts/user.md')).read_text()
        self.assertNotIn('Floor guard', prompt)


    def test_architect_is_told_framework_paths_are_not_project_source(self):
        result = self.result
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        prompt=next((self.root/'adws/adw_data/sessions/guarded').glob('project_architect/phases/*/attempt-001/user.md')).read_text()
        self.assertIn('"framework_paths"',prompt)
        self.assertIn('vendor/delegate-skills',prompt)
        self.assertIn('Installed KH tooling, not project source',prompt)


    def test_bootstrap_and_task_dispatches_persist_separately(self):
        result = self.result
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        directory=self.root/'adws/adw_data/sessions/guarded'
        setup=json.loads((directory/'execution_budget-bootstrap.json').read_text())
        task=json.loads((directory/'execution_budget-task.json').read_text())
        self.assertEqual((setup['stage'],setup['model_sends']),('bootstrap',2))
        self.assertEqual((task['stage'],task['model_sends']),('task',4))
        self.assertEqual(setup['output_corrections']+task['output_corrections'],0)
        self.assertLessEqual(setup['started_at'],task['started_at'])
        self.assertEqual(task['stop_reason'],'')
