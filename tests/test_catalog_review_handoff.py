"""Bootstrap reviews guide metadata before deriving controller-owned catalogs."""
import contextlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_project_context as helpers

pc = helpers.pc
CATALOG = 'docs/agent-guide/conditional_docs.md'
GUIDE = 'docs/agent-guide/csv.md'
CONDITIONS = [{'id': 'csv-change', 'condition': 'Read when changing CSV export.'}]


class CatalogReviewHandoffTests(unittest.TestCase):
    setUp = helpers.ContextTests.setUp
    tearDown = helpers.ContextTests.tearDown
    proposal = helpers.ContextTests.proposal
    review = helpers.ContextTests.review

    def conditional_proposal(self, content=''):
        proposal, baseline = self.proposal()
        proposal.summary = 'CSV export guidance.'
        proposal.documents.extend([
            pc.ContextDocument(path=GUIDE, content='CSV export rules.\n',
                               read_when=CONDITIONS),
            pc.ContextDocument(path=CATALOG, content=content)])
        return proposal, baseline

    def controller(self):
        @contextlib.contextmanager
        def phase(params):
            yield SimpleNamespace(phase=SimpleNamespace(params=params))
        return SimpleNamespace(repo_root=self.root, cfg=object(), phase=phase,
                               session_dir=self.root / '.kh/session', adw_id='catalog-test')

    def bootstrap(self, proposal, inspect_review, *, max_repairs=0, refresh=False):
        def execute(run, phase, call):
            if phase.params.owner == 'project_architect':
                return proposal
            brief = json.loads(call.prompt)
            return inspect_review(run, brief)
        with patch('adw_modules.agents.resolve', return_value=SimpleNamespace(writes=None)), \
             patch('adw_modules.agents.execute', side_effect=execute):
            return pc.bootstrap(self.controller(), max_repairs=max_repairs, refresh=refresh)

    def test_model_catalog_is_discarded_before_review_without_repair(self):
        proposal, _ = self.conditional_proposal('Stale catalog [bad](missing.md).')
        def approve(run, brief):
            self.assertNotIn(CATALOG, [d['path'] for d in brief['proposal']['documents']])
            self.assertNotIn('controller_normalization', brief)
            self.assertFalse((self.root / CATALOG).exists())
            return self.review(proposal)
        receipt = self.bootstrap(proposal, approve)
        self.assertIn(CATALOG, str(receipt['warnings']))
        self.assertNotIn('normalization_digest', receipt)
        index = pc.inspect_context(self.root)['index']
        self.assertEqual((self.root / CATALOG).read_text(), pc.cd.render_catalog(index['documents']))

    def test_catalog_is_derived_after_approval_without_a_model_target(self):
        proposal, _ = self.conditional_proposal()
        proposal.documents = [doc for doc in proposal.documents if doc.path != CATALOG]
        def approve(run, brief):
            self.assertNotIn(CATALOG, [d['path'] for d in brief['proposal']['documents']])
            self.assertFalse((self.root / CATALOG).exists())
            self.assertEqual(brief['proposal_digest'], pc.digest(proposal.model_dump()))
            return self.review(proposal)
        receipt = self.bootstrap(proposal, approve)
        self.assertEqual(receipt['status'], 'ready')
        index = pc.inspect_context(self.root)['index']
        self.assertEqual((self.root / CATALOG).read_text(), pc.cd.render_catalog(index['documents']))
        self.assertFalse(list((self.root / '.kh/session').glob('context-normalization-*.json')))

    def test_unrelated_findings_still_block_publication(self):
        proposal, _ = self.conditional_proposal()

        def reject(run, brief):
            self.assertNotIn(CATALOG, [d['path'] for d in brief['proposal']['documents']])
            return self.review(proposal, findings=['Loader exceptions are described incorrectly.'])

        with self.assertRaisesRegex(pc.ContextBlocked, 'Loader exceptions'):
            self.bootstrap(proposal, reject)
        self.assertFalse((self.root / CATALOG).exists())
        self.assertFalse((self.root / pc.INDEX).exists())
        self.assertEqual((self.root / 'src/main.py').read_text(), 'original = True\n')

    def test_approval_for_discarded_model_catalog_is_stale(self):
        proposal, _ = self.conditional_proposal()
        submitted_digest = pc.digest(proposal.model_dump())

        def stale_approval(run, brief):
            self.assertNotEqual(brief['proposal_digest'], submitted_digest)
            return pc.ContextReviewOutput(status='success', decision='approve',
                                          proposal_digest=submitted_digest)

        with self.assertRaisesRegex(pc.ContextBlocked, 'bind current'):
            self.bootstrap(proposal, stale_approval)
        self.assertFalse((self.root / CATALOG).exists())

    def test_conditions_changed_after_review_invalidate_publication(self):
        proposal, _ = self.conditional_proposal()

        def tamper(run, brief):
            review = self.review(proposal)
            next(d for d in proposal.documents if d.path == GUIDE).read_when[0].condition = 'Different rule.'
            return review

        with self.assertRaises(pc.ContextBlocked):
            self.bootstrap(proposal, tamper)
        self.assertFalse((self.root / CATALOG).exists())

    def test_owner_catalog_cannot_be_overwritten_by_derivation(self):
        target = self.root / CATALOG
        target.parent.mkdir(parents=True)
        target.write_text('Owner catalog must remain unchanged.\n')
        proposal, _ = self.conditional_proposal()
        with self.assertRaisesRegex(pc.ContextBlocked, 'Preserve owner documentation'):
            self.bootstrap(proposal, lambda run, brief: self.review(proposal))
        self.assertEqual(target.read_text(), 'Owner catalog must remain unchanged.\n')
        self.assertFalse((self.root / pc.INDEX).exists())

    def test_unconditional_context_does_not_create_an_unneeded_catalog(self):
        proposal, _ = self.proposal()

        def approve(run, brief):
            self.assertNotIn('controller_normalization', brief)
            return self.review(proposal)

        self.assertEqual(self.bootstrap(proposal, approve)['status'], 'ready')
        self.assertFalse((self.root / CATALOG).exists())

    def publish_initial_context(self):
        proposal, baseline = self.conditional_proposal()
        self.assertTrue(pc.validate_proposal(self.root, proposal, baseline)['valid'])
        pc.publish_context(self.root, proposal, self.review(proposal), baseline)

    def test_retained_catalog_keeps_its_bytes_when_conditions_are_unchanged(self):
        self.publish_initial_context()
        before = (self.root / CATALOG).read_bytes()
        proposal, _ = self.proposal()
        proposal.retained_paths = [GUIDE, CATALOG]

        def approve(run, brief):
            self.assertNotIn('controller_normalization', brief)
            self.assertIn(CATALOG, brief['proposal']['retained_paths'])
            return self.review(proposal)

        self.assertEqual(self.bootstrap(proposal, approve, refresh=True)['status'], 'ready')
        self.assertEqual((self.root / CATALOG).read_bytes(), before)

    def test_refresh_generation_uses_inherited_reviewed_conditions(self):
        self.publish_initial_context()
        proposal, _ = self.proposal()
        proposal.documents.extend([pc.ContextDocument(path=GUIDE, content='Updated CSV guide.\n'),
                                   pc.ContextDocument(path=CATALOG, content='')])

        def approve(run, brief):
            guide = next(d for d in brief['proposal']['documents'] if d['path'] == GUIDE)
            self.assertEqual(guide['read_when'], CONDITIONS)
            return self.review(proposal)

        self.assertEqual(self.bootstrap(proposal, approve, refresh=True)['status'], 'ready')
        self.assertIn('Read when changing CSV export.', (self.root / CATALOG).read_text())


class CatalogProviderHandoffTests(unittest.TestCase):
    def setUp(self):
        from tests import test_start as startup
        startup.StartupTests.setUp(self)
        code = startup.FAKE.replace("elif role=='plan_reviewer':", """    result['summary']='CSV export guidance.'
    result['documents'].extend([
        {'path':'docs/agent-guide/csv.md','content':'CSV guide.\\n',
         'read_when':[{'id':'csv-change','condition':'Read when changing CSV export.'}]},
        {'path':'docs/agent-guide/conditional_docs.md','content':''}])
elif role=='plan_reviewer':""")
        code = code.replace("        result.update(proposal_digest=brief['proposal_digest'],decision='approve')", """        assert 'controller_normalization' not in brief
        assert all(d['path']!='docs/agent-guide/conditional_docs.md' for d in brief['proposal']['documents'])
        guide=next(d for d in brief['proposal']['documents'] if d['path']=='docs/agent-guide/csv.md')
        assert guide['read_when']==[{'id':'csv-change','condition':'Read when changing CSV export.'}]
        evidence.write_text(json.dumps({'reviewed_conditions':guide['read_when']}))
        result.update(proposal_digest=brief['proposal_digest'],decision='approve')""")
        self.cli.write_text(code)

    def tearDown(self):
        from tests import test_start as startup
        startup.StartupTests.tearDown(self)

    def test_native_review_receives_conditions_without_a_catalog_placeholder(self):
        from tests import test_start as startup
        outcome = startup.StartupTests.invoke_bootstrap(self)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(outcome['context_status'], 'ready')
        sessions = self.root / 'adws/adw_data/sessions'
        self.assertFalse(list(sessions.glob('*/context-normalization-*.json')))
        approval = json.loads(next(sessions.glob('*/context-approval.json')).read_text())
        self.assertNotIn('normalization_digest', approval)
        self.assertIn(CATALOG, str(approval['warnings']))
        self.assertIn('Read when changing CSV export.', (self.root / CATALOG).read_text())
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 1\n')
        self.assertEqual((self.root / 'private.txt').read_text(), 'owner private\n')


if __name__ == '__main__':
    unittest.main()
