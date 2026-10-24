"""A re-run of an unchanged task reuses the earlier run's approved plan; any change plans again."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import sys
import unittest

from tests import test_start as startup
sys.path.insert(0, str(startup.ADWS))


class PlanReuseTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke

    def fail_checks_while_flagged(self):
        self.flag = self.base/'checks-fail'
        self.cfg['defaults']['quality_checks']['test'] = [sys.executable, '-c',
            "import sys; from pathlib import Path\n"
            f"sys.exit(7 if Path({str(self.flag)!r}).exists() else 0 if 'value = 2' in Path('src/app.py').read_text() else 1)"]
        self.config.write_text(json.dumps(self.cfg))

    def session(self, run):
        return self.root/'adws/adw_data/sessions'/run

    def planner_ran(self, run):
        return any(self.session(run).glob('context_handoff/*plan_1/planner'))

    def failed_first_run(self):
        self.fail_checks_while_flagged()
        self.flag.write_text('1')
        first = self.invoke()
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(self.planner_ran('guarded'))
        self.assertEqual((self.root/'src/app.py').read_text(), 'value = 1\n')
        self.flag.unlink()

    def test_rerun_after_failed_checks_reuses_the_approved_plan(self):
        self.failed_first_run()
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(), 'value = 2\n')
        self.assertFalse(self.planner_ran('second'), 'planner must not run again')
        self.assertFalse(any(self.session('second').glob('context_handoff/*plan_review_1')))
        approval = json.loads((self.session('second')/'planning/approval.json').read_text())
        first = json.loads((self.session('guarded')/'planning/approval.json').read_text())
        self.assertEqual((approval['run_id'], approval['reused_from']), ('second', 'guarded'))
        self.assertEqual(approval['reused_plan_digest'], first['plan_digest'])
        earlier = json.loads((self.session('guarded')/'planning/plan-1.json').read_text())
        reused = json.loads((self.session('second')/'planning/plan-1.json').read_text())
        self.assertEqual({**reused, 'artifacts': []}, {**earlier, 'artifacts': []})
        self.assertIn('reused the approved plan of run guarded', second.stdout + second.stderr)
        # The earlier planner's and reviewer's evidence travel with the plan into this run.
        plan = json.loads((self.session('second')/'planning/plan-1.json').read_text())
        self.assertTrue(plan['artifacts'] and all('/reused_plan_guarded/' in a for a in plan['artifacts']))
        self.assertTrue(list(self.session('second').rglob('reused_plan_guarded/**/planner/evidence.md')))
        self.assertTrue(list(self.session('second').rglob('reused_plan_guarded/**/plan_reviewer/evidence.md')))

    def test_changed_plan_reviewer_settings_plan_again(self):
        self.failed_first_run()
        for role in self.cfg['agents']:
            if role['name'] == 'plan_reviewer':
                role['thinking'] = 'medium'
        for profile in self.cfg['routing']['profiles'].values():
            profile['roles']['plan_reviewer'][0].update(thinking='medium', supported_thinking=['medium'])
        self.config.write_text(json.dumps(self.cfg))
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(self.planner_ran('second'))

    def test_changed_source_plans_again(self):
        self.failed_first_run()
        (self.root/'private.txt').write_text('owner edited between runs\n')
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(self.planner_ran('second'))

    def test_tampered_earlier_plan_is_not_reused(self):
        self.failed_first_run()
        plan_file = self.session('guarded')/'planning/plan-1.json'
        plan = json.loads(plan_file.read_text())
        plan['steps'][0]['action'] += ' and also add an admin endpoint'
        plan_file.write_text(json.dumps(plan))
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(self.planner_ran('second'))
        approval = json.loads((self.session('second')/'planning/approval.json').read_text())
        self.assertEqual(approval['reused_from'], '')

    def test_reuse_can_be_turned_off(self):
        self.failed_first_run()
        self.cfg['execution'] = {'reuse_approved_plan': False}
        self.config.write_text(json.dumps(self.cfg))
        second = self.invoke(extra=['--adw-id', 'second'])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(self.planner_ran('second'))


if __name__ == '__main__':
    unittest.main()
