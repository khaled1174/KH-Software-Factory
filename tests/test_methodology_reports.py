"""Understanding and lifecycle reports project real controller evidence."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from tests import test_project_context as contexts
from tests import test_guarded_documentation as guarded
from tests import test_corrective_release as corrective
from tests import test_project as ledger
from adw_modules import project, project_context as pc
from adw_modules.data_types import KHConfig
from adw_modules.scope import _atomic_json


class UnderstandingReceiptTests(unittest.TestCase):
    setUp=contexts.ContextTests.setUp
    tearDown=contexts.ContextTests.tearDown
    proposal=contexts.ContextTests.proposal
    review=contexts.ContextTests.review
    publish=contexts.ContextTests.publish

    def test_receipt_contains_observed_map_selection_and_honest_readiness(self):
        self.publish();packet=pc.build_context_packet(self.root,'تحديث القيمة',['src/main.py'])
        run=SimpleNamespace(repo_root=self.root,session_dir=self.root/'adws/adw_data/sessions/r1',
                            adw_id='r1',cfg=KHConfig())
        before={p:(self.root/p).read_bytes() for p in ('CLAUDE.md','AGENTS.md','src/main.py')}
        result=pc.write_understanding(run,packet)
        stored=json.loads((self.root/result['receipt_path']).read_text())
        self.assertEqual(stored['context_digest'],packet['digest'])
        self.assertIn('src',stored['source_areas'])
        self.assertEqual(stored['selection'],packet['selection'])
        self.assertEqual(stored['runtime_verification'],'not performed by context preparation')
        for p,data in before.items():self.assertEqual((self.root/p).read_bytes(),data)
        self.assertTrue((self.root/result['report_path']).is_file())


class LifecycleReportTests(unittest.TestCase):
    setUp=guarded.GuardedDocumentationTests.setUp
    tearDown=guarded.GuardedDocumentationTests.tearDown
    enable=guarded.GuardedDocumentationTests.enable
    tracked=corrective.CorrectiveReleaseTests.tracked

    def init_documented_project(self):
        project.init_project(self.root,{'schema_version':1,'project':{'id':'P','name':'تجربة'},
            'milestones':[{'id':'M','title':'المرحلة الأولى'}],'tasks':[{'id':'T1','milestone_id':'M',
            'title':'تحديث القيمة','request':'Implement value 2','deliverables':['Update value'],
            'allowed_paths':['src/app.py',*guarded.DOC_PATHS],'weight':1}]})

    def test_accepted_task_report_links_every_stage_and_requires_doc_receipt(self):
        self.enable();self.init_documented_project();result=self.tracked()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        task=project.load_task(self.root,'T1');receipt=task['receipt']
        self.assertEqual(task['status'],'completed')
        report=(self.root/'.kh/task-reports/T1.md').read_text()
        self.assertIn('تحديث القيمة',report)
        self.assertIn('Project understanding',report)
        self.assertIn('project-understanding.md',report)
        self.assertIn('documentation.json',report)
        self.assertIn('app_docs/widget.md',report)
        for key in ('plan_approval_path','documentation_path'):
            self.assertTrue((self.root/receipt[key]).is_file())
        path=project.receipt_path(self.root,'T1','tracked')
        for bad in ({k:v for k,v in receipt.items() if k!='documentation'},
                    {**receipt,'documentation':{**receipt['documentation'],'status':'not_configured'}}):
            _atomic_json(path,bad)
            with self.assertRaisesRegex(project.ProjectError,'documentation'):
                project._validate_receipt(self.root,'T1','tracked',bad)
        _atomic_json(path,receipt)

    def test_old_task_report_marks_missing_context_and_docs_as_unrecorded(self):
        task=ledger.plan()['tasks'][0]
        task={**task,'status':'pending','run_id':None,'summary':'','changed_files':[],
              'evidence':[],'blocker':None,'receipt':None,'constraints':[],'non_goals':[],'dependencies':[]}
        report=project._task_markdown(task,[])
        self.assertIn('Project understanding: not recorded',report)
        self.assertIn('Documentation: not recorded',report)
        self.assertNotIn('Documentation: published',report)

    def test_exact_completion_replay_survives_reviewed_context_and_guide_updates(self):
        self.enable();self.init_documented_project();result=self.tracked()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        receipt=project.load_task(self.root,'T1')['receipt']
        for change_content in (False,True):
            inspected=pc.inspect_context(self.root)
            baseline={'observed':inspected['observed'],'digest':pc.digest(inspected['observed'])}
            rows=inspected['index']['documents']
            guide='docs/agent-guide/widget.md'
            proposal=pc.ProjectContextProposal(status='success',baseline_digest=baseline['digest'],
                retained_paths=[row['path'] for row in rows if row['path']!=guide],
                documents=[pc.ContextDocument(path=guide,
                    content='Later reviewed widget behavior.' if change_content else (self.root/guide).read_text(),
                    topics=['widget','documentation'])])
            self.assertTrue(pc.validate_proposal(self.root,proposal,baseline)['valid'])
            review=pc.ContextReviewOutput(status='success',decision='approve',
                proposal_digest=pc.digest(proposal.model_dump()))
            pc.publish_context(self.root,proposal,review,baseline)
            self.assertEqual(pc.inspect_context(self.root)['status'],'ready')
            before=(self.root/'.kh/project.db').read_bytes()
            self.assertEqual(project.complete_task(self.root,'T1','tracked',receipt)['status'],'completed')
            self.assertEqual((self.root/'.kh/project.db').read_bytes(),before)
            # First-time acceptance still requires the exact live publication.
            with self.assertRaisesRegex(project.ProjectError,'context digest|changed after review'):
                project._validate_receipt(self.root,'T1','tracked',receipt)
        path=project.receipt_path(self.root,'T1','tracked')
        bad={**receipt,'summary':'Changed accepted claim'}
        _atomic_json(path,bad)
        with self.assertRaisesRegex(project.ProjectError,'cannot be replaced'):
            project.complete_task(self.root,'T1','tracked',bad)
        _atomic_json(path,receipt)
        _atomic_json(self.root/receipt['documentation_path'],{**receipt['documentation'],'status':'not_configured'})
        with self.assertRaisesRegex(project.ProjectError,'Saved documentation receipt differs'):
            project.complete_task(self.root,'T1','tracked',receipt)
