"""Deterministic review-loop tests; no provider calls or fabricated live claims."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import planning, scope
from adw_modules.data_types import AgentConfig, ConfigDefaults, PromptEngineering, KHConfig


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = KHConfig(defaults=ConfigDefaults(required_quality_checks=['test']), agents=[
            AgentConfig(name=role, writes=[], prompt_engineering=PromptEngineering(system='x', user='y'))
            for role in ('planner', 'plan_reviewer')])
        self.phases = []
        @contextmanager
        def phase(params):
            self.phases.append(params)
            yield SimpleNamespace(phase=SimpleNamespace(params=params), log=lambda **kw: None)
        self.run = SimpleNamespace(cfg=cfg, adw_id='test', session_dir=Path(self.temp.name), phase=phase)
        self.contract = scope.ScopeContract(original_request='Implement widget', deliverables=['Implement widget'],
                                             allowed_paths=['src/'], required_checks=['test'])
        self.packet = {'digest': 'abc', 'docs': []}
        self.fingerprint = patch.object(planning.quality, 'source_fingerprint', return_value='baseline')
        self.fingerprint.start()
        self.addCleanup(self.fingerprint.stop)

    def plan(self, action='Implement widget', responses=()):
        return planning.ReviewedPlanOutput(status='success', summary='Implement requested widget',
            acceptance_criteria=['Widget works'], allowed_paths=['src/widget.py'], required_checks=['test'],
            steps=[planning.PlanStep(id='S1', action=action, paths=['src/widget.py'], requirement_ids=['R1'],
                                     validation=['Run widget behavior test'])], requirement_coverage={'R1':['S1']},
            finding_responses=list(responses))

    def execute(self, callback, budget=2):
        with patch.object(planning.agents, 'execute', side_effect=callback):
            return planning.run_reviewed_plan(self.run, self.contract.original_request, self.contract,
                                               self.packet, max_repairs=budget)

    @staticmethod
    def review(call, **updates):
        brief = json.loads(call.prompt)
        return planning.PlanReviewOutput(status='success', summary='Independent review',
            plan_digest=brief['plan_digest'], version=brief['version'], decision=updates.pop('decision','approve'),
            **updates)

    def test_task_kind_reaches_both_roles_without_dropping_original_requirements(self):
        self.run.cfg.execution.task_kind = 'bug'
        briefs = []
        def callback(run, phase, call):
            brief = json.loads(call.prompt)
            briefs.append(brief)
            self.assertIn('task_kind', brief)
            self.assertEqual(brief['task_kind'], 'bug')
            self.assertNotIn('planning_guidance', brief)
            self.assertEqual(brief['request'], self.contract.original_request)
            self.assertEqual(brief['requirements'], planning.requirements(self.contract))
            return self.plan() if call.output_type is planning.ReviewedPlanOutput else self.review(call)
        self.execute(callback, budget=0)
        self.assertEqual(len(briefs), 2)

    def test_changed_hint_invalidates_plan_identity_without_changing_roles(self):
        before=planning._roles(self.run)
        roles=[a.model_dump() for a in self.run.cfg.agents]
        self.run.cfg.execution.task_kind='feature'
        self.assertNotEqual(planning._roles(self.run),before)
        self.assertEqual([a.model_dump() for a in self.run.cfg.agents],roles)
        with self.assertRaises(ValueError):
            KHConfig(execution={'task_kind': 'invented'})

    def test_approved_receipt_binds_plan_scope_context_and_source(self):
        def callback(run, phase, call):
            return self.plan() if call.output_type is planning.ReviewedPlanOutput else self.review(call)
        plan, approval = self.execute(callback)
        planning.assert_plan_approval(self.run, plan, approval, self.contract, self.packet)
        with self.assertRaises(planning.PlanningBlocked):
            planning.assert_plan_approval(self.run, self.plan('Changed'), approval, self.contract, self.packet)
        with self.assertRaises(planning.PlanningBlocked):
            planning.assert_plan_approval(self.run, plan, approval, self.contract, {'digest':'changed'})
        with patch.object(planning.quality, 'source_fingerprint', return_value='owner edited'):
            with self.assertRaises(planning.PlanningBlocked):
                planning.assert_plan_approval(self.run, plan, approval, self.contract, self.packet)

    def test_revision_requires_independent_closure_and_fresh_phases(self):
        calls = []
        def callback(run, phase, call):
            calls.append(call)
            if call.output_type is planning.ReviewedPlanOutput:
                return self.plan() if len(calls)==1 else self.plan('Implement widget with edge case', [
                    planning.FindingResponse(finding_id='F1', disposition='fixed', explanation='Added case', reference='S1')])
            if len(calls)==2:
                return self.review(call, decision='revise', findings=[planning.PlanFinding(id='F1', severity='major',
                    step_id='S1', evidence='Missing edge case', correction='Cover empty input')])
            return self.review(call, resolved_finding_ids=['F1'])
        _, approval = self.execute(callback)
        self.assertEqual(approval.version, 2)
        self.assertEqual([p.name for p in self.phases], ['plan_1','plan_review_1','plan_2','plan_review_2','plan_approved'])
        revision = json.loads(calls[2].prompt)
        self.assertEqual(revision['previous_plan'], self.plan().model_dump())
        self.assertEqual(revision['previous_plan_digest'], planning.digest(self.plan()))
        self.assertIsNone(calls[1].previous)
        self.assertIsNone(calls[3].previous)

    def test_stale_review_rejected(self):
        def callback(run, phase, call):
            if call.output_type is planning.ReviewedPlanOutput: return self.plan()
            return self.review(call).model_copy(update={'plan_digest':'0'*64})
        with self.assertRaisesRegex(planning.PlanningBlocked, 'stale'):
            self.execute(callback)

    def test_approval_cannot_hide_major_finding(self):
        def callback(run, phase, call):
            if call.output_type is planning.ReviewedPlanOutput: return self.plan()
            return self.review(call, findings=[planning.PlanFinding(id='F1', severity='major', evidence='Missing', correction='Add')])
        with self.assertRaisesRegex(planning.PlanningBlocked, 'unresolved'):
            self.execute(callback)

    def test_repeat_plan_stops_without_builder(self):
        def callback(run, phase, call):
            if call.output_type is planning.ReviewedPlanOutput: return self.plan()
            return self.review(call, decision='revise', findings=[planning.PlanFinding(id='F1', severity='major', evidence='Missing', correction='Add')])
        with self.assertRaisesRegex(planning.PlanningBlocked, 'no progress'):
            self.execute(callback)
        self.assertEqual(len(self.phases), 3)

    def test_invalid_coverage_and_scope_block_without_review(self):
        # The coverage map is derived from the steps, so an
        # empty map is no defect; a requirement that no step implements still is.
        uncovered = [self.plan().steps[0].model_copy(update={'requirement_ids': []})]
        for changed in ({'steps': uncovered}, {'allowed_paths':['other/file.py']},
                        {'required_checks':[]}, {'steps':[self.plan().steps[0].model_copy(update={'depends_on':['S1']})]}):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(planning.PlanningBlocked, 'Final plan revision is invalid'):
                    self.execute(lambda *args: self.plan().model_copy(update=changed), budget=0)

    def test_two_rounds_then_the_final_findings_bind_the_build(self):
        calls = []
        def callback(run, phase, call):
            calls.append(call)
            if call.output_type is planning.ReviewedPlanOutput:
                # The revision answers nothing: bookkeeping, not a reason for another round.
                return self.plan() if len(calls) == 1 else self.plan('Implement widget, revised')
            if len(calls) == 2:
                return self.review(call, decision='revise', findings=[
                    planning.PlanFinding(id='F1', severity='major', evidence='a', correction='Fix a'),
                    planning.PlanFinding(id='F2', severity='major', evidence='b', correction='Fix b')])
            return self.review(call, decision='revise', resolved_finding_ids=['F1'], findings=[
                planning.PlanFinding(id='F3', severity='blocking', evidence='c', correction='Cancel the reminder too')])
        plan, approval = self.execute(callback, budget=4)
        self.assertEqual([p.name for p in self.phases],
                         ['plan_1', 'plan_review_1', 'plan_2', 'plan_review_2', 'plan_approved'])
        self.assertEqual(approval.version, 2)
        self.assertEqual([n['id'] for n in approval.final_notes], ['F3'], 'F2 was neither closed nor repeated')
        final_brief = json.loads(calls[3].prompt)
        self.assertTrue(final_brief['final_round'])
        self.assertEqual(final_brief['unanswered_finding_ids'], ['F1', 'F2'])
        self.assertEqual(json.loads(calls[2].prompt)['open_finding_ids'], ['F1', 'F2'])
        self.assertIn('Cancel the reminder too', planning.build_brief('Implement widget', approval))
        self.assertIn('Final plan review note F3 (blocking): Cancel the reminder too', self.run.required_requirements)
        planning.assert_plan_approval(self.run, plan, approval, self.contract, self.packet)

    def test_owner_decision_still_stops_planning(self):
        def callback(run, phase, call):
            if call.output_type is planning.ReviewedPlanOutput: return self.plan()
            return self.review(call, decision='needs_owner')
        with self.assertRaisesRegex(planning.PlanningBlocked, 'owner decision'):
            self.execute(callback)

    def test_writable_roles_and_invalid_budget_rejected(self):
        for budget in (-1, 6, True):
            with self.assertRaises(planning.PlanningBlocked): self.execute(lambda *args: self.plan(), budget)
        self.run.cfg.agents[0].writes = None
        with self.assertRaisesRegex(planning.PlanningBlocked, 'writes'):
            self.execute(lambda *args: self.plan())

    def test_saved_receipt_tampering_rejected(self):
        plan, approval = self.execute(lambda run, phase, call: self.plan() if call.output_type is planning.ReviewedPlanOutput else self.review(call))
        planning.approval_path(self.run).write_text('{}')
        with self.assertRaisesRegex(planning.PlanningBlocked, 'receipt differs'):
            planning.assert_plan_approval(self.run, plan, approval, self.contract, self.packet)


if __name__ == '__main__': unittest.main()
