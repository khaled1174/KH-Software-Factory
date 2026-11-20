"""Unqualified guide restrictions need preserved owner instruction evidence."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evaluate_documentation as evaluation
from adw_modules import project_context as pc


class DependencyRuleRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = evaluation.evaluate()

    def test_unsupported_dependency_restriction_is_rejected(self):
        row = next(row for row in self.result['cases'] if row['id'] == 'bad-unsupported-restriction')
        gate = row['gates']['proposal_gate']
        self.assertFalse(gate['passed'], gate)
        self.assertIn(evaluation.GUIDE, str(gate))
        self.assertIn('owner', str(gate).lower())

    def test_existing_correct_guides_still_pass(self):
        for row in self.result['cases']:
            if not row['defective']:
                with self.subTest(guide=row['id']):
                    self.assertTrue(row['gates']['proposal_gate']['passed'], row['gates'])


class DependencyEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kh-owner-rule-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def validate(self, content, **metadata):
        document = pc.ContextDocument(path='docs/agent-guide/project.md', content=content,
            applies_to=metadata.pop('applies_to', ['src/**']), **metadata)
        pc.validate_library_claims(self.root, document)

    def owner_rule(self, path='AGENTS.md'):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# Project rules\n\n- Use only the Python standard library.\n')

    def test_cited_owner_instruction_supports_the_same_rule(self):
        self.owner_rule()
        self.validate('Use only the Python standard library (AGENTS.md:3).\n')

    def test_unchanged_owner_guide_is_preserved(self):
        path = self.root / 'docs/agent-guide/project.md'
        path.parent.mkdir(parents=True)
        path.write_text('Use only the Python standard library.\n')
        self.validate(path.read_text())

    def test_no_owner_citation_fails(self):
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner'):
            self.validate('Use only the Python standard library.\n')

    def test_import_observation_is_not_a_mandate(self):
        self.validate('Current source imports use the standard library; dependencies are permitted.\n')

    def test_qualified_task_constraint_is_not_promoted_to_project_policy(self):
        self.validate('For task T1 only, use the Python standard library.\n')

    def test_wrong_citation_line_is_rejected(self):
        self.owner_rule()
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner'):
            self.validate('Use only the Python standard library (AGENTS.md:1).\n')

    def test_negative_owner_statement_is_not_authority(self):
        (self.root / 'AGENTS.md').write_text('# Rules\n\nDo not impose a standard-library-only requirement.\n')
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner'):
            self.validate('Use only the Python standard library (AGENTS.md:3).\n')

    def test_nested_instruction_cannot_authorize_a_broader_guide(self):
        self.owner_rule('src/component/AGENTS.md')
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner'):
            self.validate('Use only the Python standard library (src/component/AGENTS.md:3).\n')

    def test_nested_instruction_can_authorize_its_directory(self):
        self.owner_rule('src/AGENTS.md')
        self.validate('Use only the Python standard library (src/AGENTS.md:3).\n')

    def test_scope_traversal_cannot_borrow_nested_authority(self):
        self.owner_rule('src/AGENTS.md')
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner'):
            self.validate('Use only the Python standard library (src/AGENTS.md:3).\n',
                          applies_to=['src/../outside/**'])

    def test_task_qualified_owner_paragraph_is_not_project_policy(self):
        (self.root / 'AGENTS.md').write_text('# Rules\n\nFor task T1 only:\nUse only the Python standard library.\n')
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner'):
            self.validate('Use only the Python standard library (AGENTS.md:4).\n')

    def test_fenced_owner_example_is_not_a_rule(self):
        (self.root / 'AGENTS.md').write_text('# Wrong example\n\n```text\nUse only the Python standard library.\n```\n')
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner'):
            self.validate('Use only the Python standard library (AGENTS.md:4).\n')

    def test_source_line_numbers_remain_correct_after_a_fenced_example(self):
        (self.root / 'AGENTS.md').write_text('# Rules\n\n```text\nExample\n```\n\nUse only the Python standard library.\n')
        self.validate('Use only the Python standard library (AGENTS.md:7).\n')

    def test_historical_record_keeps_its_text(self):
        self.validate('Use only the Python standard library.\n', purpose='change_record', status='historical')

    def test_instruction_publication_checks_location_derived_scope(self):
        self.owner_rule('src/AGENTS.md')
        (self.root / 'AGENTS.md').write_text('# Root rules\n')
        (self.root / 'CLAUDE.md').write_text('# Root navigation\n')
        observed = pc.inspect_context(self.root)['observed']
        for name in ('AGENTS.md', 'CLAUDE.md'):
            with self.subTest(name=name):
                baseline = {'observed': observed, 'digest': pc.digest(observed)}
                proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
                    retained_paths=list(observed['documents']), documents=[pc.ContextDocument(
                        path='docs/agent-guide/' + name, applies_to=['src/**'],
                        content='Use only the Python standard library (src/AGENTS.md:3).\n')])
                report = pc.validate_proposal(self.root, proposal, baseline)
                self.assertFalse(report['valid'], report)
                self.assertIn('owner', str(report['findings']))

    def test_root_instruction_supports_real_architect_publication(self):
        self.owner_rule()
        (self.root / 'CLAUDE.md').write_text('# Root navigation\n')
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'],
            retained_paths=list(observed['documents']), documents=[pc.ContextDocument(
                path='docs/agent-guide/AGENTS.md', applies_to=['src/**'],
                content='Use only the Python standard library (AGENTS.md:3).\n')])
        report = pc.validate_proposal(self.root, proposal, baseline)
        self.assertTrue(report['valid'], report)
        review = pc.ContextReviewOutput(status='success', decision='approve', proposal_digest=pc.digest(proposal.model_dump()))
        pc.publish_context(self.root, proposal, review, baseline)
        row = next(r for r in pc.inspect_context(self.root)['index']['documents'] if r['path'] == 'docs/agent-guide/AGENTS.md')
        self.assertEqual(row['applies_to'], ['docs/agent-guide/**'])

    def test_generated_instructions_cannot_self_authorize(self):
        observed = pc.inspect_context(self.root)['observed']
        baseline = {'observed': observed, 'digest': pc.digest(observed)}
        proposal = pc.ProjectContextProposal(status='success', baseline_digest=baseline['digest'], documents=[
            pc.ContextDocument(path='AGENTS.md', content='# Rules\n\nUse only the Python standard library.\n'),
            pc.ContextDocument(path='CLAUDE.md', content='Read AGENTS.md.\n')])
        review = pc.ContextReviewOutput(status='success', decision='approve', proposal_digest=pc.digest(proposal.model_dump()))
        pc.publish_context(self.root, proposal, review, baseline)
        with self.assertRaisesRegex(pc.ContextBlocked, 'owner'):
            self.validate('Use only the Python standard library (AGENTS.md:3).\n')


if __name__ == '__main__':
    unittest.main()
