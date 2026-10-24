"""Reviewed in-run path additions preserve scope, candidate and approval history."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import evidence_packet, planning, quality, scope
from adw_modules.data_types import AgentConfig, ConfigDefaults, KHConfig, PromptEngineering


class ReviewedPlanRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'src').mkdir()
        self.source = self.root / 'src/widget.py'
        self.source.write_text('value = 1\n')
        cfg = KHConfig(defaults=ConfigDefaults(required_quality_checks=['test'],
                                              protected_files=['src/protected.py']), agents=[
            AgentConfig(name=role, writes=None if role == 'builder' else [],
                        prompt_engineering=PromptEngineering(system='x', user='y'))
            for role in ('planner', 'plan_reviewer', 'builder')])
        self.phases = []
        @contextmanager
        def phase(params):
            self.phases.append(params.name)
            yield SimpleNamespace(phase=SimpleNamespace(params=params), log=lambda **kw: None)
        directory = self.root / 'adws/adw_data/sessions/run'
        self.run = SimpleNamespace(repo_root=self.root, session_dir=directory, cfg=cfg,
                                   adw_id='run', task_id='T1', phase=phase,
                                   handoff_root=directory / 'context_handoff')
        self.contract = scope.ScopeContract(original_request='Implement widget',
            deliverables=['Implement widget'], allowed_paths=['src/'], required_checks=['test'])
        self.packet = {'digest': 'context'}
        scope.capture_contract(self.run, self.contract)
        self.old_plan = self.plan()
        def initial(run, phase, call):
            if call.output_type is planning.ReviewedPlanOutput:
                return self.old_plan
            return self.review(call, findings=[planning.PlanFinding(id='F1', severity='minor',
                evidence='Existing behavior', correction='Keep empty input supported')])
        with patch.object(planning.agents, 'execute', side_effect=initial):
            self.old_plan, self.old_approval = planning.run_reviewed_plan(
                self.run, self.contract.original_request, self.contract, self.packet, max_repairs=0)
        self.run.scope_contract = scope.effective_contract(self.run, self.contract, self.old_plan.allowed_paths)
        evidence_packet.deliver_plan(self.run, self.old_plan, self.old_approval, self.run.session_dir)
        self.run.required_requirements.append('Earlier code blocker B1 remains a requirement')
        self.source.write_text('value = 2  # rejected candidate\n')

    def plan(self, *, revised=False, **updates):
        paths = ['src/widget.py', 'src/helper.py'] if revised else ['src/widget.py']
        steps = [planning.PlanStep(id='S1', action='Implement widget', paths=['src/widget.py'],
                                  requirement_ids=['R1'], validation=['Run widget test'])]
        if revised:
            steps.append(planning.PlanStep(id='S2', action='Implement helper', paths=['src/helper.py'],
                                           requirement_ids=['R1'], validation=['Run helper test'], depends_on=['S1']))
        result = planning.ReviewedPlanOutput(status='success', summary='Implement widget',
            acceptance_criteria=['Widget works', *(['Helper supports widget'] if revised else [])],
            allowed_paths=paths, required_checks=['test', 'lint'], steps=steps,
            requirement_coverage={'R1': [step.id for step in steps]})
        return result.model_copy(update=updates)

    @staticmethod
    def review(call, **updates):
        brief = json.loads(call.prompt)
        return planning.PlanReviewOutput(**{'status': 'success', 'summary': 'Independently reviewed',
            'version': brief['version'], 'plan_digest': brief['plan_digest'],
            'decision': updates.pop('decision', 'approve'), **updates})

    def revise(self, *, proposal=None, review_updates=None, paths=None, mutate=None):
        method = getattr(planning, 'revise_approved_plan', None)
        self.assertTrue(callable(method), 'Missing controller-reviewed plan revision API')
        calls = []
        def execute(run, phase, call):
            calls.append(call)
            if mutate:
                mutate()
            return (proposal or self.plan(revised=True)) if call.output_type is planning.ReviewedPlanOutput \
                else self.review(call, **(review_updates or {}))
        with patch.object(planning.agents, 'execute', side_effect=execute):
            result = method(self.run, self.old_plan, self.old_approval, self.contract, self.packet,
                            required_paths=paths or ['src/helper.py'], reason='B1: Helper is needed for the rejected candidate')
        return result, calls

    def protected_history(self):
        paths = [self.run.session_dir / 'scope.json', self.run.session_dir / 'effective_scope.json',
                 self.run.session_dir / 'planning/plan-1.json', self.run.session_dir / 'planning/review-1.json']
        paths.extend((self.run.handoff_root / 'controller_inputs/plan').glob('*.json'))
        return {path: path.read_bytes() for path in paths}

    def test_approved_revision_retains_past_and_adds_only_reviewed_boundary(self):
        history = self.protected_history()
        original_approval = (self.run.session_dir / 'planning/approval.json').read_bytes()
        candidate = self.source.read_bytes()
        (plan, approval), calls = self.revise()
        self.assertEqual(approval.version, 2)
        self.assertEqual(approval.parent_approval_digest, planning.digest(self.old_approval))
        self.assertEqual(approval.revision_paths, ['src/helper.py'])
        self.assertEqual(approval.revision_candidate_digest, quality.source_fingerprint(self.run))
        self.assertEqual(approval.source_digest, self.old_approval.source_digest)
        self.assertEqual(approval.final_notes, self.old_approval.final_notes)
        self.assertEqual(self.source.read_bytes(), candidate)
        self.assertEqual({path: path.read_bytes() for path in history}, history)
        self.assertEqual((self.run.session_dir / 'planning/approval-1.json').read_bytes(), original_approval)
        self.assertTrue((self.run.session_dir / 'planning/approval-2.json').is_file())
        self.assertEqual(len(calls), 2)
        brief = json.loads(calls[0].prompt)
        self.assertEqual(brief['previous_plan'], self.old_plan.model_dump())
        self.assertEqual(brief['required_paths'], ['src/helper.py'])
        self.assertEqual(brief['required_checks'], ['test', 'lint'])
        self.assertIn('Earlier code blocker B1 remains a requirement', self.run.required_requirements)
        planning.assert_plan_approval(self.run, plan, approval, self.contract, self.packet)
        with self.assertRaises(scope.ScopeViolation):
            scope.effective_contract(self.run, self.contract, plan.allowed_paths)
        activate = getattr(scope, 'activate_revised_contract', None)
        self.assertTrue(callable(activate), 'Missing reviewed boundary activation API')
        effective = activate(self.run, self.contract, plan, approval, self.packet)
        self.assertEqual(effective.allowed_paths, ['src/'])
        scope.check_paths(effective, ['src/helper.py'])
        self.assertEqual((self.run.session_dir / 'scope.json').read_bytes(), history[self.run.session_dir / 'scope.json'])
        record = json.loads((self.run.session_dir / 'effective_scope-revisions/2.json').read_text())
        self.assertEqual(record['parent_approval_digest'], approval.parent_approval_digest)
        evidence_packet.deliver_plan(self.run, plan, approval, self.run.session_dir)
        self.assertEqual({path: path.read_bytes() for path in history if 'controller_inputs' in path.parts},
                         {path: value for path, value in history.items() if 'controller_inputs' in path.parts})
        self.assertIn('/plan/revisions/2/plan.json', evidence_packet.plan_instruction(self.run))
        evidence_packet.verify(self.run)

    def test_rejected_stale_or_substantive_review_never_changes_current_approval(self):
        pointer = (self.run.session_dir / 'planning/approval.json').read_bytes()
        cases = [{'decision': 'revise'}, {'plan_digest': '0' * 64}, {'findings': [planning.PlanFinding(
            id='B2', severity='major', evidence='Missing', correction='Correct behavior')]}]
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(planning.PlanningBlocked):
                self.revise(review_updates=updates)
            self.assertEqual((self.run.session_dir / 'planning/approval.json').read_bytes(), pointer)
            self.assertEqual(scope.load_contract(self.run.session_dir / 'effective_scope.json').plan_paths,
                             ['src/widget.py'])
        attempts = {path: path.read_bytes() for path in (self.run.session_dir / 'planning').glob('plan-*.json')}
        (_, approved), _ = self.revise()
        self.assertEqual(approved.version, 5, 'failed reviewed revisions retain their own versions')
        self.assertEqual({path: path.read_bytes() for path in attempts}, attempts)

    def test_owner_and_role_boundaries_refuse_extra_paths_before_dispatch(self):
        for paths in (['other/helper.py'], ['src/protected.py'], ['../escape'], ['src/*.py'], ['src/widget.py']):
            with self.subTest(paths=paths), self.assertRaises((planning.PlanningBlocked, scope.ScopeViolation)):
                self.revise(paths=paths)
        builder = next(a for a in self.run.cfg.agents if a.name == 'builder')
        builder.writes = ['src/widget.py']
        with self.assertRaises((planning.PlanningBlocked, scope.ScopeViolation)):
            self.revise()

    def test_revision_cannot_drop_steps_checks_criteria_or_old_boundary(self):
        cases = [self.plan(revised=True, required_checks=['test']),
                 self.plan(revised=True, acceptance_criteria=['Helper supports widget']),
                 self.plan(revised=True, allowed_paths=['src/helper.py']),
                 self.plan(revised=True, steps=[self.plan(revised=True).steps[1]])]
        pointer = (self.run.session_dir / 'planning/approval.json').read_bytes()
        for proposal in cases:
            with self.subTest(proposal=proposal), self.assertRaises((planning.PlanningBlocked, scope.ScopeViolation)):
                self.revise(proposal=proposal)
            self.assertEqual((self.run.session_dir / 'planning/approval.json').read_bytes(), pointer)

    def test_candidate_drift_and_tampered_prior_approval_are_rejected(self):
        pointer = (self.run.session_dir / 'planning/approval.json').read_bytes()
        with self.assertRaisesRegex(planning.PlanningBlocked, 'changed'):
            self.revise(mutate=lambda: self.source.write_text('unexpected planner mutation\n'))
        self.assertEqual((self.run.session_dir / 'planning/approval.json').read_bytes(), pointer)
        (self.run.session_dir / 'planning/approval.json').write_text('{}')
        with self.assertRaises(planning.PlanningBlocked):
            self.revise()

    def test_reused_revision_carries_parent_records_into_new_worker_packet(self):
        (plan, approval), _ = self.revise()
        parent_bytes = (self.run.session_dir / 'planning/approval-1.json').read_bytes()
        self.source.write_text('value = 1\n')  # A fresh run begins from the unchanged owner tree.
        directory = self.root / 'adws/adw_data/sessions/next'
        resumed = SimpleNamespace(**{**vars(self.run), 'adw_id': 'next', 'session_dir': directory,
            'scope_contract': self.contract, 'handoff_root': directory / 'context_handoff',
            'expected_inputs': {},
            'console': SimpleNamespace(note=lambda message: None)})
        scope.capture_contract(resumed, self.contract)
        reused = planning.reuse_approved_plan(resumed, self.contract.original_request, self.contract, self.packet,
            baseline=quality.source_fingerprint(resumed), roles=planning._roles(resumed), max_repairs=1)
        self.assertIsNotNone(reused, 'the approved revision should still match the original owner tree')
        new_plan, new_approval = reused
        self.assertTrue((directory / 'planning/approval-1.json').is_file(), 'parent approval was not carried')
        self.assertEqual((directory / 'planning/approval-1.json').read_bytes(), parent_bytes)
        evidence_packet.deliver_plan(resumed, new_plan, new_approval, directory)
        parent_packet = resumed.handoff_root / 'controller_inputs/plan/revisions/2/lineage/1/approval.json'
        self.assertEqual(parent_packet.read_bytes(), parent_bytes)
        evidence_packet.verify(resumed)


if __name__ == '__main__':
    unittest.main()
