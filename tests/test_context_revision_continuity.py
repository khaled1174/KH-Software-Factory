"""Bootstrap repairs retain independently checked corrections across rounds."""
import contextlib
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_project_context as fixtures

pc = fixtures.pc
GUIDE = 'docs/agent-guide/rules.md'
LOADER = 'Loader field checks raise ValueError; malformed rows may raise other exceptions.\n'
TESTING = 'Use the controller-configured unittest check.\n'
F_LOADER = 'F-01: Correct the claim that every malformed row raises ValueError.'
F_TESTING = 'F-02: Name the actual configured test check.'


class ContextRevisionContinuityTests(unittest.TestCase):
    setUp = fixtures.ContextTests.setUp
    tearDown = fixtures.ContextTests.tearDown
    proposal = fixtures.ContextTests.proposal
    review = fixtures.ContextTests.review

    def controller(self):
        @contextlib.contextmanager
        def phase(params):
            yield SimpleNamespace(phase=SimpleNamespace(params=params))
        return SimpleNamespace(repo_root=self.root, cfg=object(), phase=phase,
                               session_dir=self.root / '.kh/session', adw_id='revision-test')

    def execute_bootstrap(self, execute, repairs=2):
        with patch('adw_modules.agents.resolve', return_value=SimpleNamespace(writes=None)), \
             patch('adw_modules.agents.execute', side_effect=execute):
            return pc.bootstrap(self.controller(), max_repairs=repairs)

    @staticmethod
    def history_review(brief, **changes):
        # Only the output fields advertised by the actual controller are used.
        fields = getattr(pc.ContextReviewOutput, 'model_fields', {})
        if 'revision_checks' not in fields:
            return changes
        context = brief['revision_context']
        return {'revision_context_digest': brief['revision_context_digest'],
                'revision_checks': [{'finding_id': row['id'], 'disposition': 'resolved',
                    'evidence': 'Read the corrected loader and configured-check statements in ' + GUIDE}
                    for row in context['findings']], **changes}

    def test_later_repair_preserves_the_previously_corrected_loader_claim(self):
        seen = []
        def execute(run, phase, call):
            brief = json.loads(call.prompt)
            seen.append((phase.params.owner, brief))
            if phase.params.owner == 'project_architect':
                prior = brief.get('revision_context', {}).get('previous_proposal')
                proposal = pc.ProjectContextProposal.model_validate(prior) if prior else self.proposal()[0]
                guide = next(d for d in proposal.documents if d.path == GUIDE)
                if not prior:
                    guide.content = 'Every malformed row raises ValueError.\n'
                if F_LOADER in brief['findings']:
                    guide.content = LOADER
                if F_TESTING in brief['findings']:
                    guide.content += TESTING
                return proposal
            proposal = pc.ProjectContextProposal.model_validate(brief['proposal'])
            content = next(d.content for d in proposal.documents if d.path == GUIDE)
            findings = [F_LOADER] if LOADER not in content else [F_TESTING] if TESTING not in content else []
            return pc.ContextReviewOutput(status='success', decision='revise' if findings else 'approve',
                proposal_digest=brief['proposal_digest'], findings=findings,
                **self.history_review(brief))
        receipt = self.execute_bootstrap(execute)
        self.assertEqual(receipt['status'], 'ready')
        self.assertEqual((self.root / GUIDE).read_text(), LOADER + TESTING)
        self.assertEqual(len(seen), 6)
        self.assertEqual((self.root / 'src/main.py').read_text(), 'original = True\n')

    def test_third_round_delivers_cumulative_findings_and_exact_previous_proposal(self):
        reviews = []
        def execute(run, phase, call):
            brief = json.loads(call.prompt)
            memory = brief['revision_context']
            self.assertEqual(brief['revision_context_digest'], pc.digest(memory))
            self.assertEqual(json.loads((run.session_dir / f"context-revision-{memory['attempt']}.json").read_text()), memory)
            if phase.params.owner == 'project_architect':
                attempt = memory['attempt']
                proposal = self.proposal()[0]
                if attempt:
                    self.assertEqual(memory['previous_proposal'], reviews[-1]['proposal'])
                    self.assertEqual(memory['previous_proposal_digest'], reviews[-1]['proposal_digest'])
                if attempt == 2:
                    self.assertEqual({f['text'] for f in memory['findings']}, {F_LOADER, F_TESTING})
                    self.assertEqual([r['attempt'] for r in memory['history']], [0, 1])
                proposal.documents[-1].content += f'Correction round {attempt}.\n'
                return proposal
            reviews.append(brief)
            attempt = memory['attempt']
            return pc.ContextReviewOutput(status='success', decision='approve' if attempt == 2 else 'revise',
                proposal_digest=brief['proposal_digest'], findings=[] if attempt == 2 else [F_LOADER if attempt == 0 else F_TESTING],
                **self.history_review(brief))
        self.assertEqual(self.execute_bootstrap(execute)['status'], 'ready')

    def review_after_one_repair(self, transform):
        def execute(run, phase, call):
            brief = json.loads(call.prompt)
            attempt = brief['revision_context']['attempt']
            if phase.params.owner == 'project_architect':
                proposal = self.proposal()[0]
                proposal.documents[-1].content += f'Correction round {attempt}.\n'
                return proposal
            if not attempt:
                return pc.ContextReviewOutput(status='success', decision='revise',
                    proposal_digest=brief['proposal_digest'], findings=[F_LOADER])
            review = pc.ContextReviewOutput(status='success', decision='approve',
                proposal_digest=brief['proposal_digest'], **self.history_review(brief))
            return transform(review, brief)
        return self.execute_bootstrap(execute, repairs=1)

    def test_missing_stale_duplicate_unknown_and_unresolved_history_cannot_publish(self):
        cases = ('missing-digest', 'stale-digest', 'missing-check', 'duplicate', 'unknown', 'unresolved')
        for case in cases:
            with self.subTest(case=case):
                def transform(review, brief):
                    if case == 'missing-digest':review.revision_context_digest = ''
                    if case == 'stale-digest':review.revision_context_digest = '0' * 64
                    if case == 'missing-check':review.revision_checks = []
                    if case == 'duplicate':review.revision_checks *= 2
                    if case == 'unknown':review.revision_checks[0].finding_id = 'unknown'
                    if case == 'unresolved':review.revision_checks[0].disposition = 'unresolved'
                    self.assertTrue(brief['revision_context']['findings'])
                    return review
                with self.assertRaises(pc.ContextBlocked):
                    self.review_after_one_repair(transform)
                self.assertFalse((self.root / pc.INDEX).exists())
                self.assertFalse((self.root / GUIDE).exists())
                self.assertEqual((self.root / 'src/main.py').read_text(), 'original = True\n')

    def test_closed_history_cannot_waive_a_current_minor_finding(self):
        def transform(review, brief):
            review.findings = ['Minor: source line citation is inaccurate.']
            return review
        with self.assertRaisesRegex(pc.ContextBlocked, 'citation'):
            self.review_after_one_repair(transform)
        self.assertFalse((self.root / pc.INDEX).exists())

    def test_history_can_correct_an_earlier_mistaken_prescription_without_owner_override(self):
        def transform(review, brief):
            review.revision_checks[0].disposition = 'superseded'
            review.revision_checks[0].evidence = 'The original source supports the corrected guide; the prior prescription was inaccurate.'
            return review
        self.assertEqual(self.review_after_one_repair(transform)['status'], 'ready')
        self.assertEqual((self.root / 'src/main.py').read_text(), 'original = True\n')

    def test_blank_historical_check_evidence_is_not_a_valid_review(self):
        with self.assertRaisesRegex(ValueError, 'evidence'):
            pc.ContextRevisionCheck(finding_id='ctx-example', disposition='resolved', evidence=' \n')

    def test_owner_change_still_blocks_after_historical_findings_are_closed(self):
        def transform(review, brief):
            (self.root / 'AGENTS.md').write_text('Owner changed the rules.\n')
            return review
        with self.assertRaises(pc.ContextBlocked):
            self.review_after_one_repair(transform)
        self.assertEqual((self.root / 'AGENTS.md').read_text(), 'Owner changed the rules.\n')
        self.assertFalse((self.root / pc.INDEX).exists())

    def test_failed_validation_is_delivered_and_rechecked_without_dispatching_review_early(self):
        owners = []
        def execute(run, phase, call):
            brief = json.loads(call.prompt); owners.append(phase.params.owner)
            attempt = brief['revision_context']['attempt']
            if phase.params.owner == 'project_architect':
                proposal = self.proposal()[0]
                if not attempt:proposal.documents[-1].content = '[missing](missing.md)\n'
                else:
                    memory = brief['revision_context']
                    self.assertIn('missing.md', memory['previous_proposal']['documents'][-1]['content'])
                    self.assertFalse(memory['history'][0]['validation']['valid'])
                    self.assertEqual(memory['findings'][0]['kind'], 'validation')
                    self.assertFalse((self.root / GUIDE).exists())
                return proposal
            return pc.ContextReviewOutput(status='success', decision='approve',
                proposal_digest=brief['proposal_digest'], **self.history_review(brief))
        self.assertEqual(self.execute_bootstrap(execute, repairs=1)['status'], 'ready')
        self.assertEqual(owners, ['project_architect', 'project_architect', 'plan_reviewer'])

    def test_repeated_finding_has_one_identity_and_keeps_all_occurrences(self):
        ids = []
        def execute(run, phase, call):
            brief = json.loads(call.prompt); memory = brief['revision_context']; attempt = memory['attempt']
            if attempt:
                row, = memory['findings']; ids.append(row['id'])
                self.assertEqual(row['attempts'], list(range(attempt)))
            if phase.params.owner == 'project_architect':
                proposal = self.proposal()[0]; proposal.documents[-1].content += f'Round {attempt}.\n'
                return proposal
            return pc.ContextReviewOutput(status='success', decision='approve' if attempt == 2 else 'revise',
                proposal_digest=brief['proposal_digest'], findings=[] if attempt == 2 else [F_LOADER],
                **self.history_review(brief))
        self.assertEqual(self.execute_bootstrap(execute)['status'], 'ready')
        self.assertEqual(len(set(ids)), 1)

    def test_persisting_history_failure_prevents_any_model_dispatch(self):
        with patch.object(pc, '_atomic', side_effect=OSError('history unavailable')), \
             patch('adw_modules.agents.resolve', return_value=SimpleNamespace(writes=None)), \
             patch('adw_modules.agents.execute') as execute:
            with self.assertRaisesRegex(OSError, 'history unavailable'):
                pc.bootstrap(self.controller())
            execute.assert_not_called()
        self.assertFalse((self.root / pc.INDEX).exists())

    def test_regression_is_rejected_even_if_an_earlier_review_called_it_fixed(self):
        def execute(run, phase, call):
            brief = json.loads(call.prompt); attempt = brief['revision_context']['attempt']
            if phase.params.owner == 'project_architect':
                proposal = self.proposal()[0]
                proposal.documents[-1].content = LOADER if attempt == 1 else f'Every malformed row raises ValueError. Round {attempt}.\n'
                return proposal
            current = next(d['content'] for d in brief['proposal']['documents'] if d['path'] == GUIDE)
            findings = [F_LOADER] if LOADER not in current else [F_TESTING]
            values = self.history_review(brief)
            for check in values.get('revision_checks', []):
                row = next(r for r in brief['revision_context']['findings'] if r['id'] == check['finding_id'])
                if row['text'] == F_LOADER and LOADER not in current:check['disposition'] = 'unresolved'
            return pc.ContextReviewOutput(status='success', decision='revise',
                proposal_digest=brief['proposal_digest'], findings=findings, **values)
        with self.assertRaisesRegex(pc.ContextBlocked, 'every malformed'):
            self.execute_bootstrap(execute)
        self.assertFalse((self.root / pc.INDEX).exists())


class ContextRevisionProviderTests(unittest.TestCase):
    def setUp(self):
        from tests import test_start as startup
        startup.StartupTests.setUp(self)
        architect = '''    memory=brief['revision_context']
    attempt=memory['attempt']
    if memory['previous_proposal']:
        result['documents']=memory['previous_proposal']['documents']
        assert memory['previous_proposal_digest']==memory['history'][-1]['proposal_digest']
    guide=next(d for d in result['documents'] if d['path']=='docs/agent-guide/architecture.md')
    if not attempt:guide['content']='Every malformed row raises ValueError.\\n'
    if F_LOADER in brief['findings']:guide['content']=LOADER
    if F_TESTING in brief['findings']:guide['content']+=TESTING
    if attempt==2:
        assert {r['text'] for r in memory['findings']}=={F_LOADER,F_TESTING}
        assert LOADER in guide['content'] and TESTING in guide['content']
'''
        review = '''        memory=brief['revision_context']
        content=next(d['content'] for d in brief['proposal']['documents'] if d['path']=='docs/agent-guide/architecture.md')
        issues=[F_LOADER] if LOADER not in content else [F_TESTING] if TESTING not in content else []
        result.update(proposal_digest=brief['proposal_digest'],decision='revise' if issues else 'approve',findings=issues)
        if mode!='missing-history' or not memory['attempt']:
            result.update(revision_context_digest=brief['revision_context_digest'],revision_checks=[
                {'finding_id':r['id'],'disposition':'resolved','evidence':'Read the current architecture guide and verified the correction.'}
                for r in memory['findings']])
'''
        code = startup.FAKE.replace("elif role=='plan_reviewer':", architect + "elif role=='plan_reviewer':")
        code = code.replace("        result.update(proposal_digest=brief['proposal_digest'],decision='approve')", review)
        constants = '\n'.join(f'{key}={value!r}' for key, value in
                              [('LOADER', LOADER), ('TESTING', TESTING), ('F_LOADER', F_LOADER), ('F_TESTING', F_TESTING)])
        code = code.replace('mode=os.environ.get', constants + '\nmode=os.environ.get')
        self.cli.write_text(code)

    def tearDown(self):
        from tests import test_start as startup
        startup.StartupTests.tearDown(self)

    def test_isolated_subprocess_retains_correction_and_publishes_exact_reviewed_revision(self):
        from tests import test_start as startup
        outcome = startup.StartupTests.invoke_bootstrap(self)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual((self.root / 'docs/agent-guide/architecture.md').read_text(), LOADER + TESTING)
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 1\n')
        self.assertEqual((self.root / 'private.txt').read_text(), 'owner private\n')
        sessions = self.root / 'adws/adw_data/sessions'
        approval_path, = sessions.glob('*/context-approval.json')
        approval = json.loads(approval_path.read_text())
        memory = json.loads((approval_path.parent / 'context-revision-2.json').read_text())
        self.assertEqual(approval['revision_context_digest'], pc.digest(memory))
        self.assertEqual({r['text'] for r in memory['findings']}, {F_LOADER, F_TESTING})
        attempts = [json.loads(p.read_text()) for p in sorted(approval_path.parent.glob('context-attempt-*.json'))]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(attempts[-1]['review']['revision_checks']), 2)
        self.assertEqual(attempts[-1]['review']['revision_context_digest'], approval['revision_context_digest'])

    def test_missing_historical_review_fails_the_real_native_gate_without_publication(self):
        from tests import test_start as startup
        outcome = startup.StartupTests.invoke_bootstrap(self, mode='missing-history')
        self.assertFalse(outcome['ok'], outcome)
        self.assertIn('revision history', outcome['error'])
        self.assertFalse((self.root / pc.INDEX).exists())
        self.assertFalse((self.root / 'docs/agent-guide/architecture.md').exists())
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 1\n')

