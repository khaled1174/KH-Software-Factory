"""Recovery through the real controller, worker subprocesses, checks and review."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

from tests import test_start as startup


BUILDER = r'''
    if mode == 'recover-timeout' and previous == 'value = 1\n':
        import time
        target.write_text('value = 2  # partial\n')
        print(json.dumps({'type':'assistant','message':{'usage':{'input_tokens':7,'output_tokens':3}}}), flush=True)
        time.sleep(10)
    if mode in ('recover-repairs', 'recover-repairs-long', 'recover-stuck'):
        match = re.search(r'round=(\d+)', previous)
        count = int(match[1]) + 1 if match else 1
        if mode == 'recover-stuck': count = 1
        target.write_text('value = 2  # round=' + str(count) + '\n')
        result['changed_files'] = ['src/app.py'] if previous != target.read_text() else []
        (output/'fake-last.json').write_text(json.dumps(result))
        print(json.dumps({'type':'result','subtype':'success','is_error':False,'total_cost_usd':1000000,'result':json.dumps(result)}))
        raise SystemExit(0)
    if mode in ('recover-plan', 'recover-plan-late', 'recover-outside'):
        planned = data('PLAN:')['allowed_paths']
        target.write_text('value = 2\n')
        if mode == 'recover-plan-late' and 'src/envelope.py' not in planned:
            count = int(re.search(r'round=(\d+)', previous)[1]) + 1 if 'round=' in previous else 1
            target.write_text('value = 2  # round=' + str(count) + '\n')
        result['changed_files'] = ['src/app.py'] if previous != target.read_text() else []
        if 'src/envelope.py' in planned:
            Path('src/envelope.py').write_text('committed_task_ids = []\n')
            result['changed_files'].append('src/envelope.py')
        (output/'fake-last.json').write_text(json.dumps(result))
        print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps(result)}))
        raise SystemExit(0)
'''

REVIEWER = r'''
    if mode in ('recover-repairs', 'recover-repairs-long', 'recover-stuck', 'recover-plan', 'recover-plan-late', 'recover-outside'):
        expected_round = 'round=5' if mode == 'recover-repairs-long' else 'round=3'
        missing = (expected_round not in Path('src/app.py').read_text() if mode.startswith('recover-r') or mode == 'recover-stuck'
                   else '[]' not in Path('src/envelope.py').read_text())
        result.update(approved=not missing,
            blocking=['[F1] Finish the accepted implementation'] if missing else [],
            findings=[{'requirement':r,'met':not missing,'evidence':'Inspected actual source and controller diff'} for r in requirements],
            scope_findings=[{'path':p,'within_scope':True,'reason':'Part of accepted task'} for p in paths])
        if missing and (mode in ('recover-plan', 'recover-outside') or
                        mode == 'recover-plan-late' and 'round=2' in Path('src/app.py').read_text()):
            needed = 'private.txt' if mode == 'recover-outside' else 'src/envelope.py'
            result['blocking'] = ['[F1] Fix missing committed_task_ids in ' + needed]
            result['repair_paths'] = [needed]
        (output/'fake-last.json').write_text(json.dumps(result))
        print(json.dumps({'type':'result','subtype':'success','is_error':False,'total_cost_usd':1000000,'result':json.dumps(result)}))
        raise SystemExit(0)
'''

PLANNER = r'''
    if mode in ('recover-plan', 'recover-plan-late', 'recover-outside') and brief.get('required_paths'):
        result['allowed_paths'] = ['src/app.py', *brief['required_paths']]
        result['steps'].append({'id':'S2','action':'Repair the missing error envelope','paths':brief['required_paths'],
            'requirement_ids':ids,'depends_on':['S1'],'validation':['Run configured test']})
        result['requirement_coverage'] = {i:['S1','S2'] for i in ids}
'''


class TaskRecoveryTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke

    def fixture(self):
        fake = startup.FAKE.replace("    if mode=='crash-signal':", BUILDER + "\n    if mode=='crash-signal':")
        fake = fake.replace("    if mode=='forgetful-review':", REVIEWER + "\n    if mode=='forgetful-review':")
        fake = fake.replace("    if brief['version']>1:", PLANNER + "\n    if brief['version']>1:")
        self.cli.write_text(fake)

    @property
    def session(self):
        return self.root / 'adws/adw_data/sessions/guarded'

    def evidence(self, role, filename):
        if filename.startswith('envelope'):
            files = sorted((self.session / role).rglob(filename))
            return [p for p in files if role != 'plan_reviewer' or 'plan_digest' in json.loads(p.read_text())]
        return sorted(p for p in self.session.rglob(filename) if role in p.parts)

    def test_actual_role_timeout_continues_partial_source_and_still_requires_review(self):
        self.fixture()
        for role in self.cfg['agents']:
            if role['name'] == 'builder': role['timeout_seconds'] = 1
        self.config.write_text(json.dumps(self.cfg))
        result = self.invoke('recover-timeout')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'status\s+✓ success', 'final console status must match accepted source and exit code')
        seen = [json.loads(p.read_text()) for p in self.evidence('builder', 'seen-before.json')]
        self.assertEqual([row['app'] for row in seen], ['value = 1\n', 'value = 2  # partial\n'])
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')
        self.assertEqual(len(self.evidence('reviewer', 'envelope-*.json')), 1)
        budget = json.loads((self.session / 'execution_budget-task.json').read_text())
        self.assertEqual(budget['recoveries'], 1)
        with sqlite3.connect(self.root / 'adws/adw_data/kh.db') as db:
            tokens, known = db.execute('SELECT total_tokens,cost_known FROM sessions WHERE adw_id=?', ('guarded',)).fetchone()
        self.assertGreaterEqual(tokens, 10)
        self.assertEqual(known, 0, 'an interrupted unpriced send must keep the total partial')

    def test_repair_window_continues_progress_and_high_cost_is_informational(self):
        self.fixture()
        result = self.invoke('recover-repairs')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('round=3', (self.root / 'src/app.py').read_text())
        self.assertEqual(len(self.evidence('builder', 'seen-before.json')), 3)
        budget = json.loads((self.session / 'execution_budget-task.json').read_text())
        self.assertEqual(budget['code_repairs'], 2)
        self.assertEqual(budget['recoveries'], 1)
        self.assertGreaterEqual(budget['model_sends'], 8)
        final_review = self.evidence('reviewer', 'envelope-*.json')[-1]
        self.assertTrue(any('F1' in f['requirement'] for f in json.loads(final_review.read_text())['findings']))
        with sqlite3.connect(self.root / 'adws/adw_data/kh.db') as db:
            cost = db.execute('SELECT total_cost FROM sessions WHERE adw_id=?', ('guarded',)).fetchone()[0]
        self.assertGreaterEqual(cost, 5000000)

    def prepare_gap(self):
        self.fixture()
        (self.root / 'src/envelope.py').write_text('committed_task_ids omitted\n')
        contract = json.loads(self.scope.read_text())
        contract['allowed_paths'] = ['src/']
        self.scope.write_text(json.dumps(contract))

    def test_three_repair_limit_continues_to_fifth_independently_accepted_build(self):
        self.fixture()
        self.cfg['routing']['profiles']['current']['max_repairs'] = 3
        self.config.write_text(json.dumps(self.cfg))
        result = self.invoke('recover-repairs-long')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('round=5', (self.root / 'src/app.py').read_text())
        self.assertEqual(len(self.evidence('builder', 'seen-before.json')), 5)
        budget = json.loads((self.session / 'execution_budget-task.json').read_text())
        self.assertEqual(budget['code_repairs'], 4)
        self.assertEqual(budget['recoveries'], 1)

    def test_missing_plan_path_requires_new_independent_approval_before_repair(self):
        self.prepare_gap()
        result = self.invoke('recover-plan')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'src/envelope.py').read_text(), 'committed_task_ids = []\n')
        plans = [json.loads(p.read_text()) for p in sorted((self.session / 'planning').glob('plan-*.json'))]
        self.assertEqual(plans[0]['allowed_paths'], ['src/app.py'])
        self.assertIn('src/envelope.py', plans[-1]['allowed_paths'])
        self.assertEqual(len(self.evidence('plan_reviewer', 'envelope-*.json')), 2)
        owner = json.loads((self.session / 'scope.json').read_text())
        self.assertEqual(owner['allowed_paths'], ['src/'])
        self.assertIsNone(owner['plan_paths'])

    def test_resumed_plan_amendment_preserves_progress_in_brief_and_owner_plan(self):
        self.prepare_gap()
        stopped = self.invoke('crash-build')
        self.assertNotEqual(stopped.returncode, 0)
        observed = self.base / 'amended-plan.json'
        script = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import adw_start
from adw_modules import resume
original = resume.write_plan
def observe(root, key, plan, progress=None):
    original(root, key, plan, progress)
    if 'src/envelope.py' in plan.allowed_paths:
        Path(sys.argv[3]).write_text(json.dumps({'progress': progress,
            'markdown': (resume.work_dir(root, key) / 'plan.md').read_text()}))
resume.write_plan = observe
raise SystemExit(adw_start.main('Implement value 2', scope_file=sys.argv[2], adw_id='resumed'))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(startup.ADWS), str(self.scope), str(observed)],
            cwd=self.root, env={**self.env, 'KH_FIXTURE_MODE': 'recover-plan'}, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(observed.read_text())
        self.assertIn('- [x] S1', plan['markdown'])
        self.assertEqual(plan['progress'], {'done': ['S1'], 'next': 'wiring the value'})
        prompts = sorted((self.root / 'adws/adw_data/sessions/resumed/builder').glob('phases/*/attempt-001/user.md'))
        self.assertTrue(prompts)
        self.assertIn('previous builder reported these plan steps done: S1', prompts[-1].read_text())
        self.assertIn('wiring the value', prompts[-1].read_text())
        self.assertEqual((self.root / 'src/envelope.py').read_text(), 'committed_task_ids = []\n')

    def test_reviewer_request_cannot_expand_original_owner_scope(self):
        self.prepare_gap()
        result = self.invoke('recover-outside')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('owner scope', result.stdout + result.stderr)
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 1\n')
        self.assertEqual((self.root / 'private.txt').read_text(), 'owner private\n')
        self.assertEqual(len(self.evidence('plan_reviewer', 'envelope-*.json')), 1)

    def test_late_plan_revision_uses_one_recovery_to_execute_its_repair(self):
        self.prepare_gap()
        self.cfg['execution'] = {'recovery_rounds': 1}
        self.config.write_text(json.dumps(self.cfg))
        result = self.invoke('recover-plan-late')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'src/envelope.py').read_text(), 'committed_task_ids = []\n')
        budget = json.loads((self.session / 'execution_budget-task.json').read_text())
        self.assertEqual(budget['recoveries'], 1)
        self.assertEqual(len(self.evidence('builder', 'seen-before.json')), 3)

    def test_unchanged_candidate_cannot_recover_forever(self):
        self.fixture()
        result = self.invoke('recover-stuck')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('RECOVERY_NO_PROGRESS', result.stdout + result.stderr)
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 1\n')
        self.assertLessEqual(len(self.evidence('builder', 'seen-before.json')), 4)

    def test_explicit_zero_recovery_preserves_bounded_failure(self):
        self.fixture()
        self.cfg['execution'] = {'recovery_rounds': 0}
        self.config.write_text(json.dumps(self.cfg))
        result = self.invoke('recover-repairs')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.evidence('builder', 'seen-before.json')), 2)
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 1\n')
