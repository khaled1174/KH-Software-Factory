"""Guarded controller deadlines, immutable run IDs and visible setup accounting."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import subprocess
import sys
import unittest
from tests import test_start as fixture


class GuardedBudgetTests(unittest.TestCase):
    setUp=fixture.StartupTests.setUp
    tearDown=fixture.StartupTests.tearDown
    invoke=fixture.StartupTests.invoke
    unchanged=fixture.StartupTests.unchanged

    def test_reused_guarded_id_cannot_mutate_previous_trace_or_budget(self):
        result=self.invoke()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        directory=self.root/'adws/adw_data/sessions/guarded'
        before={p.relative_to(directory).as_posix():p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        again=self.invoke()
        self.assertNotEqual(again.returncode,0)
        self.assertIn('RUN_ALREADY_STARTED',again.stderr)
        after={p.relative_to(directory).as_posix():p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        self.assertEqual(before,after)


    def test_light_check_and_review_failure_share_single_code_repair(self):
        fake=self.cli.read_text()
        fake=fake.replace("    if mode=='semantic':text+='unrelated_feature = True\\n'",
            "    if mode=='combined':text='value = 3\\n' if previous=='value = 1\\n' else 'value = 2\\nunrelated_feature = True\\n'\n    if mode=='semantic':text+='unrelated_feature = True\\n'")
        self.cli.write_text(fake)
        result=self.invoke(mode='combined',extra=('--mode','light'))
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged()
        task=json.loads((self.root/'adws/adw_data/sessions/guarded/execution_budget-task.json').read_text())
        self.assertEqual(task['code_repairs'],1)
        self.assertEqual(task['substantive_sends'],5)
        self.assertIn('configured repair budget',task['stop_reason'])

    def test_deadline_after_review_before_promotion_cannot_publish_source(self):
        # Run actual deterministic model subprocesses and real quality checks.
        # Move only the controller's monotonic start after evidence publication
        # to force the final promotion gate to observe an expired deadline.
        script='''import sys
sys.path.insert(0,sys.argv[1])
from adw_modules import evidence
import adw_start
original=evidence.publish_staged_evidence
def expire(run,view,*args,**kwargs):
    result=original(run,view,*args,**kwargs)
    if run.execution_budget.stage=='task':
        run.execution_budget.started_monotonic-=run.execution_budget.policy.deadline_seconds+1
    return result
evidence.publish_staged_evidence=expire
adw_start.main('Implement value 2',scope_file=sys.argv[2],adw_id='deadline')
'''
        result=subprocess.run([sys.executable,'-c',script,str(fixture.ADWS),str(self.scope)],
            cwd=self.root,env=self.env,text=True,capture_output=True,timeout=60)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('EXECUTION_DEADLINE_EXCEEDED',result.stderr)
        self.unchanged()
        directory=self.root/'adws/adw_data/sessions/deadline'
        task=json.loads((directory/'execution_budget-task.json').read_text())
        self.assertEqual(task['model_sends'],4)
        self.assertEqual(task['stop_reason'],'EXECUTION_DEADLINE_EXCEEDED')
        self.assertTrue(list(directory.rglob('controller_diff_1.patch')))


if __name__=='__main__': unittest.main()
