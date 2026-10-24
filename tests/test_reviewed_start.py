"""Acceptance boundaries for the integrated bootstrap/plan-review/start pipeline."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import sqlite3
import unittest
from tests import test_start as startup
import sys
sys.path.insert(0,str(startup.ADWS))
from adw_modules import project
from adw_modules.scope import _atomic_json
from tests import test_corrective_release as corrective

class ReviewedStartTests(unittest.TestCase):
    setUp=startup.StartupTests.setUp
    tearDown=startup.StartupTests.tearDown
    invoke=startup.StartupTests.invoke
    unchanged=startup.StartupTests.unchanged
    init_plan=corrective.CorrectiveReleaseTests.init_plan
    tracked=corrective.CorrectiveReleaseTests.tracked
    def test_owner_decision_in_plan_review_never_dispatches_builder(self):
        result=self.invoke('plan-owner')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged()
        mapping=json.loads((self.root/'adws/adw_data/sessions/guarded/agent_map.json').read_text())
        self.assertNotIn('builder',mapping)
        self.assertFalse((self.root/'adws/adw_data/sessions/guarded/planning/approval.json').exists())

    def test_final_review_notes_bind_builder_and_code_review_after_two_rounds(self):
        result=self.invoke('plan-reject')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        run=self.root/'adws/adw_data/sessions/guarded'
        self.assertEqual(sorted(p.name for p in (run/'planning').glob('review-*.json')),['review-1.json','review-2.json'])
        approval=json.loads((run/'planning/approval.json').read_text())
        self.assertEqual((approval['version'],[n['id'] for n in approval['final_notes']]),(2,['F1']))
        builder_prompt=next(run.glob('builder/phases/*/attempt-001/user.md')).read_text()
        self.assertIn('Final plan review notes',builder_prompt)
        self.assertIn('State the expected value',builder_prompt)
        reviewer_prompt=next(run.glob('reviewer/phases/*/attempt-001/user.md')).read_text()
        self.assertIn('Final plan review note F1 (major): State the expected value',reviewer_prompt)
    def test_incoherent_coverage_map_is_derived_from_the_steps_without_a_correction(self):
        # A revision whose requirement_coverage no longer
        # mirrors its steps used to fail the whole run. The map is now derived from the steps.
        result=self.invoke('plan-incoherent')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        run=self.root/'adws/adw_data/sessions/guarded'
        plan_2=sorted(run.glob('planner/phases/*_plan_2/attempt-*/user.md'))
        self.assertEqual([p.parent.name for p in plan_2],['attempt-001'],'no correction send was needed')
        derived=json.loads((run/'planning/coverage-derived-2.json').read_text())
        self.assertIn('S9',json.dumps(derived['planner_stated']))
        self.assertNotIn('S9',json.dumps(derived['derived_from_steps']))
        approved=json.loads((run/'planning/plan-2.json').read_text())
        self.assertNotIn('S9',json.dumps(approved['requirement_coverage']))
        self.assertEqual(json.loads((run/'planning/approval.json').read_text())['version'],2)
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')

    def test_revision_is_reviewed_and_only_approved_version_builds(self):
        result=self.invoke('plan-repair')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        run=self.root/'adws/adw_data/sessions/guarded'
        approval=json.loads((run/'planning/approval.json').read_text())
        self.assertEqual(approval['version'],2)
        self.assertEqual(json.loads((run/'planning/review-2.json').read_text())['decision'],'approve')
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')
        self.assertTrue((self.root/'CLAUDE.md').is_file())
        self.assertTrue((self.root/'AGENTS.md').is_file())
    def test_stale_review_digest_never_dispatches_builder(self):
        result=self.invoke('stale-plan-review')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged()
        self.assertIn('stale digest',result.stderr)
    def test_missing_reviewer_choice_fails_before_model_or_bootstrap(self):
        for profile in self.cfg['routing']['profiles'].values():del profile['roles']['plan_reviewer']
        self.config.write_text(json.dumps(self.cfg))
        result=self.invoke()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('plan_reviewer',result.stderr)
        self.assertFalse((self.root/'adws/adw_data').exists())
        self.assertFalse((self.root/'CLAUDE.md').exists())
    def test_tracked_approval_binds_task_and_cannot_downgrade_receipt(self):
        self.init_plan();result=self.tracked()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        task=project.load_task(self.root,'T1')
        receipt=task['receipt'];self.assertEqual(receipt['schema_version'],2)
        approval=json.loads((self.root/receipt['plan_approval_path']).read_text())
        self.assertEqual(approval['task_id'],'T1')
        recorded=[l.split(': ',1)[1] for l in self.intent.read_text().splitlines() if l.startswith('Accepted digest: ')]
        self.assertEqual(receipt['owner_review'],{'enabled':True,'mode':'report','flagged':[],'approved_categories':[],
            'intent_path':'intents/change-value.md','intent_accepted_digest':recorded[0],'intent_binding':'explicit'})
        self.assertEqual(receipt['floor_guard'],[])
        self.assertEqual(set(receipt['cost']),{'usd','complete','tokens','note'})
        self.assertIn('## Cost',(self.root/'.kh/task-reports/T1.md').read_text())
        self.assertIn('cost never stops a task',(self.root/'.kh/task-reports/T1.md').read_text())
        invalid={**receipt,'schema_version':1}
        _atomic_json(project.receipt_path(self.root,'T1','tracked'),invalid)
        with self.assertRaisesRegex(project.ProjectError,'schema-2'):
            project.complete_task(self.root,'T1','tracked',invalid)
    def test_plan_failure_does_not_inflate_project_progress(self):
        self.init_plan();result=self.tracked(mode='plan-owner')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(project.load_task(self.root,'T1')['status'],'blocked')
        self.assertRegex(project.load_task(self.root,'T1')['blocker'],r'\[Cost: \$\d+\.\d\d( \(partial\))?, [\d,]+ tokens\]$')
        self.assertEqual(project.load_task(self.root,'T2')['status'],'pending')
        self.unchanged()

    def test_visual_adapter_failure_blocks_promotion_even_with_exit_zero(self):
        script = "import os,json;from pathlib import Path;p=Path(os.environ['KH_VERIFICATION_RESULT']);(p.parent/'capture.png').write_bytes(b'capture');p.write_text(json.dumps({'schema_version':1,'candidate_sha256':os.environ['KH_CANDIDATE_SHA256'],'status':'passed','affected_surfaces':json.loads(os.environ['KH_CHANGED_PATHS']),'covered_surfaces':json.loads(os.environ['KH_CHANGED_PATHS']),'evidence':['capture.png'],'discovered_tests':1,'executed_tests':1,'unresolved_differences':1}))"
        self.cfg['defaults']['verification_adapters']=[{'name':'visual','kind':'visual','applicable_paths':['src/**'],'argv':[sys.executable,'-c',script]}]
        self.config.write_text(json.dumps(self.cfg))
        result=self.invoke()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged()
        captures=list((self.root/'adws/adw_data/sessions/guarded/context_handoff').rglob('capture.png'))
        self.assertTrue(captures,'failed visual evidence must survive discarded source')
        mapping=json.loads((self.root/'adws/adw_data/sessions/guarded/agent_map.json').read_text())
        self.assertNotIn('reviewer',mapping,'code review cannot bypass failed visual check')

    def test_visual_evidence_reaches_independent_code_reviewer(self):
        script = "import os,json;from pathlib import Path;p=Path(os.environ['KH_VERIFICATION_RESULT']);(p.parent/'capture.png').write_bytes(b'capture');p.write_text(json.dumps({'schema_version':1,'candidate_sha256':os.environ['KH_CANDIDATE_SHA256'],'status':'passed','affected_surfaces':json.loads(os.environ['KH_CHANGED_PATHS']),'covered_surfaces':json.loads(os.environ['KH_CHANGED_PATHS']),'evidence':['capture.png'],'discovered_tests':1,'executed_tests':1,'unresolved_differences':0}))"
        self.cfg['defaults']['verification_adapters']=[{'name':'visual','kind':'visual','applicable_paths':['src/**'],'argv':[sys.executable,'-c',script]}]
        self.config.write_text(json.dumps(self.cfg));result=self.invoke()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        prompts=list((self.root/'adws/adw_data/sessions/guarded/reviewer/phases').rglob('user.md'))
        self.assertTrue(prompts)
        self.assertTrue(all('Controller verification evidence' in p.read_text() and 'capture.png' in p.read_text() and 'candidate_sha256' in p.read_text() for p in prompts))

    def test_bootstrap_requires_current_phase_evidence(self):
        result=self.invoke('fake-context-evidence')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('phase evidence',result.stderr)
        self.assertFalse((self.root/'CLAUDE.md').exists())
        self.unchanged()

    def test_owner_questions_stop_project_context_after_one_architect_send(self):
        result=self.invoke('owner-questions')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('Owner decisions needed before project context can be prepared',result.stderr)
        self.assertIn('Which JWT signing key rotation policy applies?',result.stderr)
        run=self.root/'adws/adw_data/sessions/guarded'
        self.assertEqual(len(list(run.glob('project_architect/phases/*'))),1,'no repair round for owner questions')
        self.assertFalse(list(run.glob('plan_reviewer/phases/*')),'nothing to review without the owner')
        self.assertFalse((self.root/'CLAUDE.md').exists())
        self.unchanged()

    def test_a_forgotten_evidence_file_is_corrected_in_the_same_session(self):
        result=self.invoke('architect-forgets-evidence')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        run=self.root/'adws/adw_data/sessions/guarded'
        attempts=sorted(p.name for p in run.glob('project_architect/phases/*/attempt-*'))
        self.assertEqual(attempts,['attempt-001','attempt-002'],'one correction send, not a new round')
        self.assertTrue((self.root/'CLAUDE.md').is_file())
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')


    def test_installed_package_runs_reviewed_start_from_installed_modules(self):
        import subprocess
        installed=subprocess.run([sys.executable,str(startup.ROOT/'install.py'),'--project',str(self.root),'--upgrade'],
            text=True,capture_output=True,timeout=30)
        self.assertEqual(installed.returncode,0,installed.stdout+installed.stderr)
        # Owner-configured deterministic provider and real quality command.
        self.config.write_text(json.dumps(self.cfg))
        result=subprocess.run([sys.executable,str(self.root/'adws/adw_start.py'),'--scope-file',str(self.scope),
            '--adw-id','installed','--','Implement value 2'],cwd=self.root,env=self.env,
            text=True,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(),'value = 2\n')
        self.assertTrue((self.root/'adws/adw_data/sessions/installed/planning/approval.json').is_file())
