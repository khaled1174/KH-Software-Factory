"""Closing docs through real guarded-start CLI fixtures, without real providers."""
import json
from pathlib import Path
import unittest
import sys
from tests import test_start as startup
sys.path.insert(0, str(startup.ADWS))

DOC_PATHS = ['app_docs/widget.md', 'docs/agent-guide/widget.md']
DOC_BRANCH = r'''
    result['documentation']={'decision':'not_needed' if mode=='docs-not-needed' else 'required',
        'reason':'Internal value only; no documented interface changes' if mode=='docs-not-needed' else 'Document the new value and update the current guide',
        'paths':[] if mode=='docs-not-needed' else ['app_docs/widget.md','docs/agent-guide/widget.md']}
    if mode!='docs-not-needed':result['allowed_paths']+=['app_docs/widget.md','docs/agent-guide/widget.md']
elif role=='documenter':
    brief=json.JSONDecoder().raw_decode(user)[0]
    if mode=='docs-worker-write':Path('src/app.py').write_text('worker touched source')
    documents=[{'path':p,'content':'Widget now returns value = 2. Run the configured test.\n',
        'purpose':'change_record' if p.startswith('app_docs/') else 'guide',
        'status':'historical' if p.startswith('app_docs/') else 'active',
        'applies_to':['src/**'],'topics':['widget']} for p in brief['documentation']['paths']]
    if mode=='docs-source-proposal':documents[0]['path']='src/app.py'
    if mode=='docs-empty':documents=[]
    if mode=='docs-invalid-history':documents[0]['status']='active'
    if mode=='docs-broken-link':documents[1]['content']='See [missing](absent.md)'
    if mode=='docs-too-large':documents[1]['content']='x'*131071
    result.update(candidate_sha256='0'*64 if mode=='docs-stale' else brief['candidate_sha256'],
        documents=documents)
elif role=='builder':
'''
FAKE = startup.FAKE.replace('planner|builder|reviewer)', 'planner|builder|reviewer|documenter)').replace("elif role=='builder':\n", DOC_BRANCH)

FAKE = FAKE.replace("outside='unrelated_feature' in Path('src/app.py').read_text()", "outside='unrelated_feature' in Path('src/app.py').read_text() or mode=='docs-review-reject'")
FAKE = FAKE.replace("result={'status':'success','summary':role,'artifacts':[str(evidence)]}\n", "result={'status':'success','summary':role,'artifacts':[str(evidence)]}\nif role=='reviewer' and os.environ.get('KH_DOC_REVIEW_CRASH')=='1':\n    print('killed by the role timeout',file=sys.stderr);raise SystemExit(124)\n")


class GuardedDocumentationTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke
    unchanged = startup.StartupTests.unchanged

    def enable(self):
        self.cli.write_text(FAKE)
        self.cfg['execution']={'documentation':{'enabled':True},'recovery_rounds':0}
        role=dict(next(a for a in self.cfg['agents'] if a['name']=='reviewer'))
        role['name']='documenter';role['writes']=['**']
        system=self.base/'documenter-system.md';system.write_text('ROLE documenter')
        role['prompt_engineering']={**role['prompt_engineering'],'system':str(system)}
        self.cfg['agents'].append(role)
        for profile in self.cfg['routing']['profiles'].values():
            profile['roles']['documenter']=[{'coding_agent':'claude_code','model':'sonnet','thinking':'high','supported_thinking':['high']}]
        self.config.write_text(json.dumps(self.cfg))
        contract=json.loads(self.scope.read_text());contract['allowed_paths']+=DOC_PATHS
        self.scope.write_text(json.dumps(contract))

    def session(self):
        return self.root/'adws/adw_data/sessions/guarded'

    def receipt(self):
        return json.loads((self.session()/'documentation.json').read_text())

    def test_docs_checked_reviewed_promoted_and_loaded_for_next_task(self):
        self.enable()
        self.cfg['defaults']['quality_checks']['test']=[startup.sys.executable,'-c',
            "from pathlib import Path; assert 'value = 2' in Path('src/app.py').read_text(); assert 'value = 2' in Path('docs/agent-guide/widget.md').read_text(); assert Path('app_docs/widget.md').is_file()"]
        self.config.write_text(json.dumps(self.cfg));result=self.invoke()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        for path in DOC_PATHS:self.assertIn('value = 2',(self.root/path).read_text())
        receipt=self.receipt();self.assertEqual(receipt['status'],'published')
        self.assertEqual(receipt['decision'],'required')
        self.assertEqual(set(receipt['paths']),set(DOC_PATHS))
        from adw_modules import project_context as pc
        packet=pc.build_context_packet(self.root,'edit widget',['src/app.py'])
        loaded={d['path']:d['content'] for d in packet['documents']}
        self.assertIn('value = 2',loaded['docs/agent-guide/widget.md'])
        self.assertNotIn('app_docs/widget.md',loaded)
        mapping=json.loads((self.session()/'agent_map.json').read_text())
        self.assertNotEqual(mapping['documenter']['session_id'],mapping['builder']['session_id'])
        self.assertTrue(receipt['checks_passed']);self.assertTrue(receipt['review_approved'])
        for artifact in receipt['artifacts']:
            self.assertFalse(Path(artifact).is_absolute())
            self.assertTrue((self.root/artifact).is_file())

    def test_enabled_stage_requires_explicit_documenter_choice_before_dispatch(self):
        self.enable()
        for profile in self.cfg['routing']['profiles'].values():profile['roles'].pop('documenter',None)
        self.config.write_text(json.dumps(self.cfg))
        result=self.invoke();self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('documenter',result.stdout+result.stderr);self.unchanged()
        self.assertFalse((self.root/'adws/adw_data').exists())

    def test_not_needed_is_explicit_without_documenter_dispatch(self):
        self.enable();result=self.invoke('docs-not-needed')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        receipt=self.receipt();self.assertEqual(receipt['decision'],'not_needed')
        self.assertTrue(receipt['reason']);self.assertFalse((self.root/DOC_PATHS[0]).exists())
        self.assertNotIn('documenter',json.loads((self.session()/'agent_map.json').read_text()))

    def test_legacy_run_reports_stage_unconfigured(self):
        result=self.invoke();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.receipt()['status'],'not_configured')

    def test_required_missing_role_stops_before_builder(self):
        self.enable();self.cfg['agents']=[a for a in self.cfg['agents'] if a['name']!='documenter']
        self.config.write_text(json.dumps(self.cfg))
        result=self.invoke();self.assertNotEqual(result.returncode,0)
        self.assertIn('documenter',result.stdout+result.stderr)
        self.unchanged()
        self.assertFalse((self.root/'adws/adw_data').exists())

    def test_bad_proposals_and_worker_source_write_cannot_promote(self):
        for mode in ('docs-source-proposal','docs-empty','docs-stale','docs-invalid-history','docs-worker-write','docs-broken-link','docs-too-large'):
            with self.subTest(mode=mode):
                # Each mode gets a separate task/session and owner root.
                case=GuardedDocumentationTests();case.setUp()
                try:
                    case.enable();result=case.invoke(mode)
                    self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                    case.unchanged()
                    self.assertFalse((case.root/DOC_PATHS[0]).exists())
                finally:case.tearDown()

    def test_failed_check_keeps_docs_out_of_owner_tree(self):
        self.enable();self.cfg['defaults']['quality_checks']['test']=[startup.sys.executable,'-c','raise SystemExit(7)']
        self.config.write_text(json.dumps(self.cfg));result=self.invoke()
        self.assertNotEqual(result.returncode,0);self.unchanged()
        self.assertFalse((self.root/DOC_PATHS[0]).exists())

    def test_existing_owner_guide_cannot_be_overwritten(self):
        self.enable();p=self.root/DOC_PATHS[1];p.parent.mkdir(parents=True);p.write_text('Owner exact rule')
        result=self.invoke();self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged();self.assertEqual(p.read_text(),'Owner exact rule')

    def test_review_rejection_does_not_publish_docs(self):
        self.enable();result=self.invoke('docs-review-reject')
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr);self.unchanged()
        self.assertFalse((self.root/DOC_PATHS[0]).exists())

    def test_checked_docs_survive_review_only_continuation_without_worker(self):
        self.enable();self.env['KH_DOC_REVIEW_CRASH']='1'
        first=self.invoke();self.assertNotEqual(first.returncode,0,first.stdout+first.stderr)
        self.unchanged()
        meta=json.loads(next((self.root/'.kh/work').glob('*/current/meta.json')).read_text())
        self.assertEqual(meta['checked']['documentation']['decision'],'required')
        self.env.pop('KH_DOC_REVIEW_CRASH')
        second=self.invoke(extra=['--adw-id','reviewonly','--review-existing'])
        self.assertEqual(second.returncode,0,second.stdout+second.stderr)
        mapping=json.loads((self.root/'adws/adw_data/sessions/reviewonly/agent_map.json').read_text())
        self.assertNotIn('documenter',mapping);self.assertNotIn('builder',mapping)
        self.assertIn('value = 2',(self.root/DOC_PATHS[1]).read_text())

    def test_previously_reviewed_managed_guide_can_be_updated(self):
        self.enable()
        from adw_modules import project_context as pc
        observed=pc.inspect_context(self.root)['observed']
        baseline={'observed':observed,'digest':pc.digest(observed)}
        proposal=pc.ProjectContextProposal(status='success',baseline_digest=baseline['digest'],documents=[
            pc.ContextDocument(path='CLAUDE.md',content='Read the current guide.'),
            pc.ContextDocument(path='AGENTS.md',content='Preserve owner scope.'),
            pc.ContextDocument(path=DOC_PATHS[1],content='Widget returns value = 1.',applies_to=['src/**'])])
        review=pc.ContextReviewOutput(status='success',proposal_digest=pc.digest(proposal.model_dump()),decision='approve')
        pc.publish_context(self.root,proposal,review,baseline)
        result=self.invoke();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('value = 2',(self.root/DOC_PATHS[1]).read_text())

    def seed_scoped_guide(self, *, in_owner_scope=False):
        from adw_modules import project_context as pc
        guide = 'docs/agent-guide/interface.md'
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'], documents=[
            pc.ContextDocument(path='CLAUDE.md', content='Preserve owner scope.'),
            pc.ContextDocument(path='AGENTS.md', content='Preserve owner scope.'),
            pc.ContextDocument(path=guide, content='The public interface remains stable.\n', applies_to=['src/app.py'])])
        pc.publish_context(self.root, proposal, pc.ContextReviewOutput(status='success',
            proposal_digest=pc.digest(proposal.model_dump()), decision='approve'), baseline)
        if in_owner_scope:
            contract = json.loads(self.scope.read_text()); contract['allowed_paths'].append(guide)
            self.scope.write_text(json.dumps(contract))
        return guide

    def test_documenter_confirms_affected_guide_without_writing_outside_approved_targets(self):
        self.enable()
        from adw_modules import project_context as pc
        guide = self.seed_scoped_guide(in_owner_scope=True)
        target = self.root / guide
        before = (target.read_bytes(), target.stat().st_mtime_ns)
        self.cli.write_text(FAKE.replace("    if mode=='docs-source-proposal':", """
    result['confirmed_documents']=[{key: row[key] for key in ('path','digest','source_digest')}
        for row in brief['affected_guides'] if row['path'] not in brief['documentation']['paths']]
    if mode=='docs-source-proposal':"""))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        self.assertEqual([row['path'] for row in self.receipt()['confirmed_documents']], [guide])
        self.assertNotIn(guide, self.receipt()['paths'])
        self.assertEqual(len(list((self.session() / 'context_handoff').glob('*_document_1'))), 1)
        # A different accepted ticket can invalidate this real confirmation.
        from adw_modules import documentation
        (self.root / 'src/app.py').write_text('value = 3\n')
        with self.assertRaisesRegex(ValueError, 'Guide confirmation differs'):
            documentation.validate_source_proofs(type('View', (), {'repo_root': self.root})(), self.receipt())

    def test_outside_scope_guide_is_preserved_and_recorded_after_real_publication(self):
        self.enable()
        from adw_modules import project_context as pc
        guide = self.seed_scoped_guide()
        original = (self.root / guide).read_bytes()
        contract = self.scope.read_bytes()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.scope.read_bytes(), contract)
        self.assertEqual((self.root / guide).read_bytes(), original)
        self.assertEqual(self.receipt()['confirmed_documents'], [])
        self.assertEqual(self.receipt()['stale_source_conflicts'], ['Guide source changed: '+guide+' <- src/app.py'])
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')
        self.assertEqual(self.receipt()['stale_source_conflicts'], pc.inspect_context(self.root)['conflicts'])
        self.assertNotIn(guide, self.receipt()['paths'])

    def test_next_task_regenerates_deferred_guide_through_independent_context_review(self):
        self.enable()
        from adw_modules import project_context as pc
        guide = self.seed_scoped_guide()
        (self.root / 'src/app.py').write_text('value = 2  # already implemented\n')
        contract = json.loads(self.scope.read_text()); contract['allowed_paths'].append(guide)
        self.scope.write_text(json.dumps(contract))
        self.assertEqual(pc.inspect_context(self.root)['status'], 'stale')
        self.cli.write_text(FAKE.replace("elif role=='plan_reviewer':", """
    result['documents'].append({'path':'docs/agent-guide/interface.md',
        'content':'Current source: '+Path('src/app.py').read_text(), 'applies_to':['src/app.py']})
elif role=='plan_reviewer':""").replace("    if mode=='docs-source-proposal':",
            "    result['confirmed_documents']=[{key:row[key] for key in ('path','digest','source_digest')} for row in brief['affected_guides'] if row['path'] not in brief['documentation']['paths']]\n    if mode=='docs-source-proposal':"))
        result = self.invoke(extra=['--adw-id','next-task','--refresh-context'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(pc.inspect_context(self.root)['status'], 'ready')
        self.assertIn('value = 2', (self.root / guide).read_text())
        session = self.root / 'adws/adw_data/sessions/next-task'
        approval = json.loads((session / 'context-approval.json').read_text())
        self.assertEqual(approval['status'], 'ready')
        roles = json.loads((session / 'agent_map.json').read_text())
        self.assertIn('project_architect', roles)
        self.assertIn('plan_reviewer', roles)

    def test_unscoped_documentation_planning_includes_relevant_guides_without_granting_paths(self):
        self.enable()
        guide = self.seed_scoped_guide()
        from types import SimpleNamespace
        from adw_modules import documentation, scope
        contract = scope.ScopeContract(original_request='Update the current behavior', deliverables=['Current behavior'])
        self.assertIsNone(contract.allowed_paths)
        brief = documentation.planning_contract(SimpleNamespace(repo_root=self.root,
            cfg=SimpleNamespace(execution=SimpleNamespace(documentation=SimpleNamespace(enabled=True))),
            scope_contract=contract))
        self.assertIn(guide, {row['path'] for row in brief['affected_guides']})
        self.assertIsNone(contract.allowed_paths)

    def test_missing_wrong_duplicate_or_writable_confirmations_cannot_promote(self):
        changes = {
            'missing': '',
            'wrong': "    result['confirmed_documents'][0]['source_digest']='0'*64\n",
            'duplicate': "    result['confirmed_documents']*=2\n",
            'write': "    documents.append({'path':result['confirmed_documents'][0]['path'],'content':'Unapproved update'})\n",
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                case = GuardedDocumentationTests(); case.setUp()
                try:
                    case.enable(); guide = case.seed_scoped_guide(in_owner_scope=True)
                    original = (case.root / guide).read_bytes()
                    if name != 'missing':
                        injected = "    result['confirmed_documents']=[{key:row[key] for key in ('path','digest','source_digest')} for row in brief['affected_guides']]\n"
                        case.cli.write_text(FAKE.replace("    if mode=='docs-source-proposal':", injected + change + "    if mode=='docs-source-proposal':"))
                    result = case.invoke()
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    case.unchanged()
                    self.assertEqual((case.root / guide).read_bytes(), original)
                finally:
                    case.tearDown()

    def test_documenter_content_update_preserves_omitted_historical_metadata(self):
        self.enable()
        from adw_modules import project_context as pc
        observed=pc.inspect_context(self.root)['observed']
        baseline={'observed':observed,'digest':pc.digest(observed)}
        proposal=pc.ProjectContextProposal(status='success',baseline_digest=baseline['digest'],documents=[
            pc.ContextDocument(path='CLAUDE.md',content='Preserve owner scope.'),
            pc.ContextDocument(path='AGENTS.md',content='Preserve owner scope.'),
            pc.ContextDocument(path=DOC_PATHS[1],content='Old widget behavior.',
                purpose='change_record',status='historical',applies_to=['legacy/**'])])
        review=pc.ContextReviewOutput(status='success',proposal_digest=pc.digest(proposal.model_dump()),decision='approve')
        pc.publish_context(self.root,proposal,review,baseline)
        # New app_docs entries still supply their mandatory metadata explicitly.
        self.cli.write_text(FAKE.replace("    if mode=='docs-source-proposal':",
            "    for row in documents:\n        if row['path'].startswith('docs/agent-guide/'):\n            for key in ('purpose','status','applies_to','topics'):row.pop(key,None)\n    if mode=='docs-source-proposal':"))
        result=self.invoke();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        row=next(row for row in self.receipt()['documents'] if row['path']==DOC_PATHS[1])
        self.assertEqual((row['purpose'],row['status']),('change_record','historical'))
        self.assertEqual(row['applies_to'],['legacy/**'])
        packet=pc.build_context_packet(self.root,'widget',['src/app.py'])
        self.assertNotIn(DOC_PATHS[1],{row['path'] for row in packet['documents']})


class DocumentationPublicationTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from tests import test_project_context as contexts
        from adw_modules import documentation as docs, permissions, quality, scope
        from adw_modules.data_types import KHConfig, AgentConfig
        self.helper=contexts.ContextTests();self.helper.setUp();self.addCleanup(self.helper.tearDown)
        self.root=self.helper.root;self.helper.publish()
        self.docs=docs;self.pc=contexts.pc
        cfg=KHConfig(agents=[AgentConfig(name='builder',writes=None,prompt_engineering={'system':'unused','user':'unused'})])
        self.run=SimpleNamespace(repo_root=self.root,cfg=cfg,adw_id='test',
            session_dir=self.root/'adws/adw_data/sessions/test',
            scope_contract=scope.ScopeContract(original_request='Document value',deliverables=['Document behavior'],allowed_paths=['docs/agent-guide/new.md']))
        self.run.task_baseline=permissions.snapshot(self.run)
        raw=self.pc._read(self.root,self.pc.INDEX)
        doc=self.pc.ContextDocument(path='docs/agent-guide/new.md',content='Current behavior',applies_to=['src/**'])
        self.pc._atomic(self.root,doc.path,doc.content)
        self.receipt={'schema_version':1,'status':'candidate','decision':'required','reason':'Behavior changed',
            'candidate_sha256':quality.source_fingerprint(self.run),'paths':[doc.path],
            'prior_index_digest':self.pc.digest(raw),'documents':[{'path':doc.path,'digest':self.pc.digest(doc.content),
                'applies_to':doc.applies_to,**self.pc.lifecycle(doc.path,doc.model_dump())}]}
        self.receipt['source_proofs'] = docs._source_proofs(self.run, self.receipt['documents'], json.loads(raw))
        self.projection=docs.context_projection(self.run,self.receipt)
        self.original_index=(self.root/self.pc.INDEX).read_bytes()

    def test_owner_drift_blocks_context_publication_and_records_recovery(self):
        (self.root/'docs/agent-guide/new.md').write_text('Owner edited after promotion')
        with self.assertRaisesRegex(ValueError,'Promoted source differs'):
            self.docs.finish(self.run,self.receipt,self.projection)
        self.assertEqual((self.root/self.pc.INDEX).read_bytes(),self.original_index)
        record=json.loads((self.run.session_dir/'documentation.json').read_text())
        self.assertEqual(record['status'],'publication_failed')
        self.assertIn('reconcile',record['recovery'])

    def test_owner_index_edit_and_pending_cleanup_are_never_overwritten(self):
        for pending in (False,True):
            with self.subTest(pending=pending):
                if pending:self.run.role_cleanup_pending={'kh_cleanup':{'complete':False}}
                else:(self.root/self.pc.INDEX).write_text('{"owner":"changed"}')
                before=(self.root/self.pc.INDEX).read_bytes()
                with self.assertRaises((ValueError,RuntimeError)):
                    self.docs.finish(self.run,self.receipt,self.projection)
                self.assertEqual((self.root/self.pc.INDEX).read_bytes(),before)

    def test_projection_is_bound_to_actual_document_bytes(self):
        (self.root/'docs/agent-guide/new.md').write_text('Different candidate')
        with self.assertRaisesRegex(ValueError,'candidate changed'):
            self.docs.context_projection(self.run,self.receipt)

    def test_successful_projection_loads_current_guide_and_keeps_baseline_owner_rules(self):
        result=self.docs.finish(self.run,self.receipt,self.projection)
        self.assertEqual(result['status'],'published')
        packet=self.pc.build_context_packet(self.root,'behavior',['src/main.py'])
        self.assertIn('docs/agent-guide/new.md',{d['path'] for d in packet['documents']})
        self.assertEqual((self.root/'AGENTS.md').read_text(),'Read [rules](docs/agent-guide/rules.md).\n')


    def test_missing_or_tampered_source_proof_fails_but_unrelated_source_is_allowed(self):
        (self.root / 'unrelated.py').write_text('unrelated = True\n')
        self.docs.validate_source_proofs(self.run, self.receipt)
        for proofs in ({}, {'docs/agent-guide/new.md': {'digest': '0' * 64}}):
            with self.subTest(proofs=proofs), self.assertRaisesRegex(ValueError, 'source proof'):
                self.docs.validate_source_proofs(self.run, {**self.receipt, 'source_proofs': proofs})

    def test_disabled_or_not_needed_documentation_leaves_existing_binding_stale(self):
        from types import SimpleNamespace
        self.docs.finish(self.run, self.receipt, self.projection)
        original_index = (self.root / self.pc.INDEX).read_bytes()
        (self.root / 'src/main.py').write_text('original = False\n')
        for enabled in (False, True):
            self.run.cfg.execution.documentation.enabled = enabled
            plan = SimpleNamespace(documentation=self.docs.DocumentationPlan(
                decision='not_needed', reason='No documentation target approved', paths=[]))
            receipt = self.docs.prepare_candidate(self.run, plan, ['src/main.py'])
            self.assertIsNone(self.docs.context_projection(self.run, receipt))
            self.docs.finish(self.run, receipt, None)
            self.assertEqual((self.root / self.pc.INDEX).read_bytes(), original_index)
            self.assertEqual(self.pc.inspect_context(self.root)['status'], 'stale')


class DocumentationNativeContractTests(unittest.TestCase):
    def test_documenter_gets_controller_only_proposal_mode(self):
        from adw_modules import agents, documentation
        from adw_modules.data_types import AgentConfig
        agent=AgentConfig(name='documenter',coding_agent='claude_code',tools=['read'],writes=[],
            prompt_engineering={'system':'unused','user':'unused'})
        text=agents._runtime_instructions(None,agent,documentation.DocumentationProposalOutput)
        self.assertIn('GUARDED DOCUMENTATION PROPOSAL',text)
        self.assertIn('Only the controller may publish exact approved paths',text)


class RecordedDocumentationContractTests(unittest.TestCase):
    """Documenter responses with candidate/evidence identities rebound in isolation."""
    changed = ['app_docs/latency.md', 'docs/reports/latency.md',
               'logreports/reports/latency.py', 'tests/test_latency.py']

    def setUp(self):
        from types import SimpleNamespace
        from tests import test_project_context as contexts
        from adw_modules import documentation, permissions, scope
        from adw_modules.data_types import AgentConfig, KHConfig
        self.docs = documentation
        self.helper = contexts.ContextTests(); self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.root = self.helper.root
        for name in self.changed[1:]:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Before the measured change.\n')
        self.helper.publish()
        prompt = self.root / '.kh/replay-prompt.md'; prompt.write_text('Unused provider fixture.\n')
        self.cli = self.root / '.kh/replay-cli'; self.cli.write_text('#!/bin/sh\nexit 97\n'); self.cli.chmod(0o700)
        prompts = {'system': str(prompt), 'user': str(prompt)}
        choice = {'coding_agent': 'claude_code', 'model': 'fixture', 'thinking': 'medium',
                  'supported_thinking': ['medium']}
        cfg = KHConfig(routing={'default_profile': 'fixture', 'profiles': {
            'fixture': {'roles': {'documenter': [choice]}}}}, agents=[
            AgentConfig(name='builder', writes=None, prompt_engineering=prompts),
            AgentConfig(name='documenter', model='fixture', tools=['read'], writes=[], prompt_engineering=prompts)])
        cfg.execution.documentation.enabled = True
        self.run = SimpleNamespace(repo_root=self.root, cfg=cfg, adw_id='replay',
            scope_contract=scope.ScopeContract(original_request='Document the latency report',
                deliverables=['Latency change record'], allowed_paths=[*self.changed, 'app_docs/extra.md']),
            context_handoff_dir=self.root / 'adws/adw_data/sessions/replay/context_handoff/documenter')
        self.run.context_handoff_dir.mkdir(parents=True)
        self.artifact = self.run.context_handoff_dir / 'notes.md'; self.artifact.write_text('Replay evidence.\n')
        import contextlib
        @contextlib.contextmanager
        def phase(params):
            yield SimpleNamespace(phase=SimpleNamespace(params=params))
        self.run.phase = phase
        self.run.task_baseline = permissions.snapshot(self.run)
        for name in self.changed:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('After the measured change.\n')
        self.plan = SimpleNamespace(allowed_paths=list(self.run.scope_contract.allowed_paths),
            documentation=self.docs.DocumentationPlan(
                decision='required', reason='Document the latency implementation', paths=[self.changed[0]]))

    def payload(self, attempt):
        import hashlib
        from adw_modules import quality, terminal_evidence
        expected = {'001': '61e3f860eccd849ede86db4358255e17350bacd2429cfc8ca39c293f99b5458a',
                    '002': 'dcf58396172aa4336ef6a1dfa3b2857db205398716bfeafd010a99056ff6938b'}
        path = startup.ROOT / 'tests/fixtures/documentation_contract' / f'latency-attempt-{attempt}.txt'
        raw = path.read_bytes(); self.assertEqual(hashlib.sha256(raw).hexdigest(), expected[attempt])
        data = terminal_evidence.extract_json(raw.decode())
        data['candidate_sha256'] = quality.source_fingerprint(self.run)
        data['artifacts'] = [str(self.artifact)]
        return data

    def prepare(self, payload):
        from unittest.mock import patch
        from adw_modules import agents, agent_cc
        def execute(run, phase, call):
            output = call.output_type.model_validate(payload)
            for gate in call.gates:
                report = gate(output, run)
                if not report.passed:
                    raise ValueError('; '.join(report.violations))
            return output
        with patch.object(agent_cc, 'CC_PATH', str(self.cli)), \
                patch.dict('os.environ', {'CLAUDE_CODE_PATH': str(self.cli)}), \
                patch.object(agents, 'execute', execute):
            return self.docs.prepare_candidate(self.run, self.plan, self.changed)

    def test_recorded_incomplete_claim_uses_controller_coverage_without_mutating_payload(self):
        payload = self.payload('001'); before = json.dumps(payload, sort_keys=True)
        receipt = self.prepare(payload)
        self.assertEqual(receipt['documented_paths'], self.changed)
        self.assertEqual(receipt['paths'], ['app_docs/latency.md'])
        self.assertEqual((self.root / receipt['paths'][0]).read_text(), payload['documents'][0]['content'])
        self.assertEqual(json.dumps(payload, sort_keys=True), before)

    def test_recorded_corrected_reply_still_rejects_named_unapproved_document(self):
        before = (self.root / self.changed[0]).read_bytes()
        with self.assertRaisesRegex(ValueError, r'extra=.*docs/reports/latency\.md'):
            self.prepare(self.payload('002'))
        self.assertEqual((self.root / self.changed[0]).read_bytes(), before)

    def test_exact_target_gate_names_missing_and_duplicate_paths(self):
        self.plan.documentation.paths.append('app_docs/extra.md')
        with self.assertRaisesRegex(ValueError, r'missing=.*app_docs/extra\.md'):
            self.prepare(self.payload('001'))
        self.plan.documentation.paths.pop()
        payload = self.payload('001'); payload['documents'] *= 2
        with self.assertRaisesRegex(ValueError, r'duplicate=.*app_docs/latency\.md'):
            self.prepare(payload)

    def test_model_contract_omits_coverage_and_still_forbids_unknown_fields(self):
        schema = self.docs.DocumentationProposalOutput.model_json_schema()
        self.assertNotIn('documented_paths', schema['properties'])
        payload = self.payload('001'); payload.pop('documented_paths')
        self.assertEqual(self.prepare(payload)['documented_paths'], self.changed)
        payload['unapproved_output_field'] = True
        with self.assertRaisesRegex(ValueError, 'extra_forbidden'):
            self.docs.DocumentationProposalOutput.model_validate(payload)

    def test_wrong_candidate_remains_rejected_before_document_publication(self):
        payload = self.payload('001'); payload['candidate_sha256'] = '0' * 64
        before = (self.root / self.changed[0]).read_bytes()
        with self.assertRaisesRegex(ValueError, 'measured candidate'):
            self.prepare(payload)
        self.assertEqual((self.root / self.changed[0]).read_bytes(), before)


class DeferredDocumentationAcceptanceTests(unittest.TestCase):
    """Out-of-scope guide updates with real inspection and receipt bindings."""
    def setUp(self):
        import tempfile
        from types import SimpleNamespace
        from adw_modules import documentation, planning, project, project_context, scope
        self.docs, self.project, self.pc = documentation, project, project_context
        self.temp = tempfile.TemporaryDirectory(dir=str(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture = Path(__file__).parent / 'fixtures/documentation_contract/status-counts-outside-scope.json'
        self.record = json.loads(fixture.read_text())
        self.guide = self.record['guide']['path']
        self.source = self.record['source_path']
        self.plan = planning.ReviewedPlanOutput.model_validate(self.record['plan'])
        contract = scope.ScopeContract.model_validate(self.record['scope'])
        self.run = SimpleNamespace(repo_root=self.root, scope_contract=contract)
        (self.root / self.source).parent.mkdir(parents=True)
        (self.root / self.source).write_text(self.record['source_content'])
        target = self.plan.documentation.paths[0]
        (self.root / target).parent.mkdir(parents=True)
        (self.root / target).write_text(self.record['replies'][0]['documents'][0]['content'])
        baseline = self.pc.inspect_context(self.root)
        proposal = self.pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'], documents=[
            self.pc.ContextDocument(path='CLAUDE.md', content='Preserve owner scope.'),
            self.pc.ContextDocument(path='AGENTS.md', content='Preserve owner scope.'),
            self.pc.ContextDocument(path=self.guide, content=self.record['guide_content'],
                applies_to=self.record['guide']['applies_to'])])
        self.pc.publish_context(self.root, proposal, self.pc.ContextReviewOutput(status='success',
            proposal_digest=self.pc.digest(proposal.model_dump()), decision='approve'), baseline)
        self.prior = json.loads((self.root / self.pc.INDEX).read_text())
        self.assertEqual(self.pc.inspect_context(self.root)['status'], 'ready')
        (self.root / self.source).write_text('def summarize(records):\n    return {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}\n')
        inspected = self.pc.inspect_context(self.root)
        self.assertEqual(inspected['conflicts'], ['Guide source changed: ' + self.guide + ' <- ' + self.source])
        self.publication = {'status':'published', 'decision':'required', 'checks_passed':True,
            'review_approved':True, 'candidate_sha256':'a'*64, 'run_id':'recorded-status-counts',
            'paths':[target], 'documents':[{'path':target,'digest':self.pc.digest((self.root / target).read_text())}],
            'context_digest':inspected['digest'], 'stale_source_conflicts':inspected['conflicts']}
        self.receipt = {'documentation':self.publication, 'documentation_path':'.kh/documentation.json',
            'source_sha256':'a'*64, 'run_id':'recorded-status-counts'}
        self.approved = self.plan.model_dump()

    def validate(self):
        (self.root / self.receipt['documentation_path']).write_text(json.dumps(self.publication))
        self.project._validate_documentation_receipt(self.root, self.receipt, self.approved)

    def test_recorded_status_counts_outside_scope_is_accepted(self):
        for reply in self.record['replies']:
            with self.subTest(recorded_status=reply['status']):
                documents = [self.pc.ContextDocument.model_validate(row) for row in reply['documents']]
                self.docs._validate_confirmations(self.run, self.prior, documents, [], [self.source])
        self.validate()
        self.assertNotIn(self.guide, self.run.scope_contract.allowed_paths)
        self.assertEqual(self.pc.inspect_context(self.root)['status'], 'stale')
        self.assertEqual((self.root / self.guide).read_text(), self.record['guide_content'])

    def test_owner_boundary_is_consistent_in_staging_and_publication(self):
        from adw_modules.workspace import RunView
        self.run.scope_contract = self.run.scope_contract.model_copy(update={'allowed_paths':None})
        staged = RunView(self.run, scope_contract=self.run.scope_contract.model_copy(
            update={'allowed_paths':self.plan.allowed_paths, 'plan_paths':self.plan.allowed_paths}))
        for view in (self.run, staged):
            with self.subTest(staged=view is staged), self.assertRaisesRegex(ValueError, self.guide):
                documents = [self.pc.ContextDocument.model_validate(row) for row in self.record['replies'][0]['documents']]
                self.docs._validate_confirmations(view, self.prior, documents, [], [self.source])

    def test_unrecorded_wrong_cause_and_other_context_conflicts_are_rejected(self):
        expected = list(self.publication['stale_source_conflicts'])
        for recorded in ([], ['Guide source changed: '+self.guide+' <- tests/test_status_counts.py'],
                         expected*2, None, {}, [42]):
            with self.subTest(recorded=recorded):
                self.publication['stale_source_conflicts'] = recorded
                with self.assertRaisesRegex(self.project.ProjectError, '^Published documentation context digest differs$'):
                    self.validate()
        self.publication['stale_source_conflicts'] = expected
        (self.root / 'unregistered.py').write_text('unrelated = True\n')
        with self.assertRaisesRegex(self.project.ProjectError, '^Published documentation context digest differs$'):
            self.validate()

    def test_stale_required_guide_is_rejected(self):
        self.approved['documentation']['paths'].append(self.guide)
        self.publication['paths'].append(self.guide)
        self.publication['documents'].append({'path':self.guide, 'digest':self.pc.digest((self.root/self.guide).read_text())})
        with self.assertRaisesRegex(self.project.ProjectError, '^Published documentation context digest differs$'):
            self.validate()

    def test_different_context_digest_is_rejected(self):
        self.publication['context_digest'] = '0'*64
        with self.assertRaisesRegex(self.project.ProjectError, '^Published documentation context digest differs$'):
            self.validate()
