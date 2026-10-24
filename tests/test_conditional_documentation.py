"""Two-folder conditional documentation through the real guarded CLI, fake providers."""
import json
from pathlib import Path
import unittest
from tests import test_guarded_documentation as gd
from tests import test_start as startup
from tests import test_conditional_docs as ch
from adw_modules import project_context as pc

CATALOG = ch.CATALOG
GUIDE = gd.DOC_PATHS[1]
PATHS = [*gd.DOC_PATHS, CATALOG]

FAKE = gd.FAKE.replace(repr(gd.DOC_PATHS).replace(', ', ','), repr(PATHS).replace(', ', ','))
FAKE = FAKE.replace("    result['documentation']={", """    result['context_selection']=[{'path':row['path'],'document_digest':row['digest'],
        'decision':'read' if row['path']=='docs/agent-guide/widget.md' else 'skip',
        'condition_ids':[row['read_when'][0]['id']] if row['path']=='docs/agent-guide/widget.md' else [],
        'reason':'Task changes widget behavior' if row['path']=='docs/agent-guide/widget.md' else 'No payment behavior changes'}
        for row in brief['context'].get('conditional_documents',[])]
    result['documentation']={""")
FAKE = FAKE.replace("    if mode=='docs-source-proposal':", """    for row in documents:
        if row['path']=='docs/agent-guide/widget.md':
            row['read_when']=[{'id':'widget-change','condition':'Read only when changing widget behavior or fixing widget failures'}]
        if row['path']=='docs/agent-guide/conditional_docs.md':row['content']=''
    if mode=='docs-source-proposal':""")
FAKE = FAKE.replace("    target=Path('src/app.py')", """    (output/'context-seen.json').write_text(json.dumps({'widget':'Widget now returns value = 2' in user,
        'payment':'PAYMENT PRIVATE GUIDE' in user}))
    target=Path('src/app.py')""")


class ConditionalDocumentationTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke
    unchanged = startup.StartupTests.unchanged
    session = gd.GuardedDocumentationTests.session
    receipt = gd.GuardedDocumentationTests.receipt

    def enable(self):
        gd.GuardedDocumentationTests.enable(self)
        self.cli.write_text(FAKE)
        contract = json.loads(self.scope.read_text())
        contract['allowed_paths'].append(CATALOG)
        self.scope.write_text(json.dumps(contract))

    def test_catalog_checked_promoted_then_selected_on_next_real_run(self):
        self.enable()
        self.cfg['defaults']['quality_checks']['test']=[startup.sys.executable,'-c',
            "from pathlib import Path; assert 'value = 2' in Path('src/app.py').read_text(); "
            "assert 'widget-change' in Path('docs/agent-guide/conditional_docs.md').read_text(); "
            "assert Path('app_docs/widget.md').is_file()"]
        self.config.write_text(json.dumps(self.cfg))
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stdout+first.stderr)
        self.assertEqual(set(self.receipt()['paths']), set(PATHS))
        row = next(row for row in self.receipt()['documents'] if row['path']==GUIDE)
        self.assertEqual(row['read_when'][0]['id'], 'widget-change')
        packet = pc.build_context_packet(self.root, 'Change widget', ['src/app.py'])
        self.assertNotIn(GUIDE, {row['path'] for row in packet['documents']})
        self.assertNotIn('app_docs/widget.md', {row['path'] for row in packet['documents']})
        # Seed a real second delta without claiming the existing value-2 guide
        # describes value 1. Renew its review against the comment-only change.
        (self.root/'src/app.py').write_text('value = 2  # next fixture delta\n')
        inspection=pc.inspect_context(self.root)
        baseline={'observed':inspection['observed'], 'digest':pc.digest(inspection['observed'])}
        proposal=pc.ProjectContextProposal(status='success',baseline_digest=baseline['digest'],
            retained_paths=[row['path'] for row in inspection['index']['documents'] if row['path'] not in {CATALOG, GUIDE}],
            documents=[pc.ContextDocument(path=CATALOG,content=''),
                pc.ContextDocument(path=GUIDE,content=(self.root/GUIDE).read_text()),
                pc.ContextDocument(path=ch.PAYMENTS,content='PAYMENT PRIVATE GUIDE',applies_to=['src/**'],
                    read_when=[{'id':'payments-change','condition':'Read only when changing payments'}])])
        self.assertTrue(pc.validate_proposal(self.root,proposal,baseline)['valid'])
        review=pc.ContextReviewOutput(status='success',proposal_digest=pc.digest(proposal.model_dump()),decision='approve')
        pc.publish_context(self.root,proposal,review,baseline)
        second = self.invoke('docs-not-needed', extra=['--adw-id','next'])
        self.assertEqual(second.returncode, 0, second.stdout+second.stderr)
        session=self.root/'adws/adw_data/sessions/next'
        seen=json.loads(next(session.rglob('builder/context-seen.json')).read_text())
        self.assertTrue(seen['widget']);self.assertFalse(seen['payment'])
        understanding=json.loads((session/'project-understanding.json').read_text())
        self.assertIn(GUIDE, {row['path'] for row in understanding['documents']})
        decisions={row['path']:row['decision'] for row in understanding['conditional_selection']['decisions']}
        self.assertEqual(decisions,{GUIDE:'read',ch.PAYMENTS:'skip'})

    def test_unplanned_catalog_cannot_be_written(self):
        self.enable()
        self.cli.write_text(FAKE.replace(
            repr(PATHS).replace(', ', ','), repr(gd.DOC_PATHS).replace(', ', ',')))
        result=self.invoke()
        self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('conditional_docs.md', result.stdout+result.stderr)
        self.unchanged()
        self.assertFalse((self.root/CATALOG).exists())

    def test_failed_checks_leave_all_documentation_unpublished(self):
        self.enable()
        self.cfg['defaults']['quality_checks']['test']=[startup.sys.executable,'-c','raise SystemExit(7)']
        self.config.write_text(json.dumps(self.cfg))
        result=self.invoke();self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.unchanged()
        for path in PATHS:self.assertFalse((self.root/path).exists())

    def test_review_only_continuation_retains_checked_conditions_and_catalog(self):
        self.enable();self.env['KH_DOC_REVIEW_CRASH']='1'
        first=self.invoke();self.assertNotEqual(first.returncode,0,first.stdout+first.stderr)
        self.unchanged();self.env.pop('KH_DOC_REVIEW_CRASH')
        second=self.invoke(extra=['--adw-id','reviewonly','--review-existing'])
        self.assertEqual(second.returncode,0,second.stdout+second.stderr)
        mapping=json.loads((self.root/'adws/adw_data/sessions/reviewonly/agent_map.json').read_text())
        self.assertNotIn('builder',mapping);self.assertNotIn('documenter',mapping)
        self.assertIn('widget-change',(self.root/CATALOG).read_text())
        self.assertEqual(pc.inspect_context(self.root)['status'],'ready')

    def test_review_only_rejects_lost_conditions_in_saved_metadata(self):
        self.enable();self.env['KH_DOC_REVIEW_CRASH']='1'
        first=self.invoke();self.assertNotEqual(first.returncode,0,first.stdout+first.stderr)
        meta_path=next((self.root/'.kh/work').glob('*/current/meta.json'))
        metadata=json.loads(meta_path.read_text())
        for row in metadata['checked']['documentation']['documents']:
            row.pop('read_when',None)
        meta_path.write_text(json.dumps(metadata))
        self.env.pop('KH_DOC_REVIEW_CRASH')
        second=self.invoke(extra=['--adw-id','reviewonly','--review-existing'])
        self.assertNotEqual(second.returncode,0,second.stdout+second.stderr)
        self.assertIn('catalog',second.stdout+second.stderr)
        self.unchanged()
        for path in PATHS:self.assertFalse((self.root/path).exists())


if __name__ == '__main__':
    unittest.main()
