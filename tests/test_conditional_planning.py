"""Selection is reviewed before building and bound to exact planning inputs."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import unittest
from tests import test_planning_review as ph
planning = ph.planning
from tests import test_conditional_docs as ch
CSV, PAYMENTS = ch.CSV, ch.PAYMENTS


class ConditionalPlanningTests(unittest.TestCase):
    plan = ph.PlanningTests.plan
    execute = ph.PlanningTests.execute
    review = staticmethod(ph.PlanningTests.review)

    def setUp(self):
        ph.PlanningTests.setUp(self)
        context = ch.ConditionalDocsTests()
        context.setUp()
        self.addCleanup(context.tearDown)
        self.packet = context.publish()
        self.run.repo_root = context.root
        self.run.cfg.agents.append(ph.AgentConfig(name='builder', writes=['src/**'],
            prompt_engineering=ph.PromptEngineering(system='x', user='y')))
        self.run.session_dir = Path(self.temp.name) / 'sessions/first'
        self.run.session_dir.mkdir(parents=True)
        self.run.console = type('Console', (), {'note': lambda *args: None})()
        self.choices = context.choices(self.packet)
        self.run.project_context_packet = self.packet

    def selected_plan(self, **updates):
        return planning.ReviewedPlanOutput.model_validate({**self.plan().model_dump(),
                'context_selection': self.choices, **updates})

    def test_review_and_later_roles_receive_only_chosen_bodies(self):
        frozen = copy.deepcopy(self.packet)
        def callback(run, phase, call):
            brief = json.loads(call.prompt)
            if call.output_type is planning.ReviewedPlanOutput:
                self.assertIn('conditional_context_contract', brief)
                self.assertNotIn('CSV CURRENT GUIDE', json.dumps(brief))
                return self.selected_plan()
            self.assertEqual(brief['context'], run.project_context_packet)
            self.assertIn('CSV CURRENT GUIDE', json.dumps(brief))
            self.assertNotIn('PAYMENT CURRENT GUIDE', json.dumps(brief))
            return self.review(call)
        plan, approval = self.execute(callback)
        self.assertEqual(self.packet, frozen)
        self.assertIn(CSV, {row['path'] for row in self.run.project_context_packet['documents']})
        self.assertNotIn(PAYMENTS, {row['path'] for row in self.run.project_context_packet['documents']})
        receipt = json.loads((Path(self.run.session_dir)/'planning/context-selection.json').read_text())
        self.assertEqual(receipt['decisions'], self.choices)
        planning.assert_plan_approval(self.run, plan, approval, self.contract, self.packet)

    def test_missing_selection_never_reaches_review(self):
        calls=[]
        def callback(run, phase, call):
            calls.append(call.output_type)
            return self.plan()
        with self.assertRaisesRegex(planning.PlanningBlocked, 'selection|catalog'):
            self.execute(callback, budget=0)
        self.assertNotIn(planning.PlanReviewOutput, calls)

    def test_semantically_rejected_selection_cannot_become_final_build_note(self):
        def callback(run, phase, call):
            if call.output_type is planning.ReviewedPlanOutput:
                return self.selected_plan()
            return self.review(call, decision='revise', findings=[planning.PlanFinding(
                id='CONTEXT-1', severity='blocking', evidence='CSV applies, but payments also applies',
                correction='Read both guides and revise the plan')])
        with self.assertRaisesRegex(planning.PlanningBlocked, 'conditional|selection'):
            self.execute(callback, budget=0)
        self.assertFalse(planning.approval_path(self.run).exists())

    def test_rejected_round_restores_pending_packet_for_replanning(self):
        calls=[]
        def callback(run, phase, call):
            calls.append(call.output_type)
            if call.output_type is planning.ReviewedPlanOutput:
                self.assertEqual(run.project_context_packet, self.packet)
                if len(calls)==1:
                    return self.selected_plan()
                return self.selected_plan(summary='Revised task context',
                    risks=['CSV failure also needs a regression test'])
            return self.review(call, **({'decision':'revise', 'findings':[planning.PlanFinding(
                id='C1', severity='major', evidence='Incomplete plan', correction='Add regression risk')]}
                if len(calls)==2 else {'resolved_finding_ids':['C1']}))
        self.execute(callback)

    def test_baseline_content_used_after_candidate_guide_edits(self):
        from types import SimpleNamespace
        entry = SimpleNamespace(kind='file', data=b'CSV CURRENT GUIDE')
        self.run.task_baseline = SimpleNamespace(entries={CSV:entry})
        (self.run.repo_root / CSV).write_text('Candidate guide update')
        self.execute(lambda run, phase, call: self.selected_plan()
                     if call.output_type is planning.ReviewedPlanOutput else self.review(call))
        loaded = next(row['content'] for row in self.run.project_context_packet['documents'] if row['path']==CSV)
        self.assertEqual(loaded, 'CSV CURRENT GUIDE')

    def test_changed_selection_invalidates_approval(self):
        plan, approval = self.execute(lambda run, phase, call: self.selected_plan()
                     if call.output_type is planning.ReviewedPlanOutput else self.review(call))
        plan.context_selection[0].reason = 'A different justification'
        with self.assertRaises(planning.PlanningBlocked):
            planning.assert_plan_approval(self.run, plan, approval, self.contract, self.packet)

    def test_legacy_plan_dump_keeps_original_identity(self):
        self.assertNotIn('context_selection', self.plan().model_dump())

    def test_reuse_delivers_saved_selection_and_rejects_changed_guide(self):
        plan, approval = self.execute(lambda run, phase, call: self.selected_plan()
                     if call.output_type is planning.ReviewedPlanOutput else self.review(call))
        later = copy.copy(self.run)
        later.adw_id = 'second'
        later.session_dir = Path(self.run.session_dir).parent / 'second'
        later.project_context_packet = self.packet
        reused = planning.reuse_approved_plan(later, self.contract.original_request, self.contract,
            self.packet, baseline='baseline', roles=planning._roles(later), max_repairs=0)
        self.assertIsNotNone(reused)
        self.assertIn('CSV CURRENT GUIDE', json.dumps(later.project_context_packet))
        (self.run.repo_root / CSV).write_text('Owner changed the guide')
        self.assertIsNone(planning.reuse_approved_plan(later, self.contract.original_request, self.contract,
            self.packet, baseline='baseline', roles=planning._roles(later), max_repairs=0))

    def test_reviewed_revision_preserves_selection_delivery(self):
        old, approval = self.execute(lambda run, phase, call: self.selected_plan()
                     if call.output_type is planning.ReviewedPlanOutput else self.review(call))
        revised = self.selected_plan(allowed_paths=['src/widget.py','src/helper.py'])
        def callback(run, phase, call):
            if call.output_type is planning.ReviewedPlanOutput:
                self.assertEqual(run.project_context_packet, self.packet)
                return revised
            self.assertIn('CSV CURRENT GUIDE', json.dumps(json.loads(call.prompt)['context']))
            return self.review(call)
        with patch.object(planning.agents, 'execute', side_effect=callback):
            result, receipt = planning.revise_approved_plan(self.run, old, approval, self.contract,
                self.packet, required_paths=['src/helper.py'], reason='Helper needed by widget')
        self.assertEqual(receipt.version, 2)
        self.assertEqual(result.context_selection, old.context_selection)


if __name__ == '__main__':
    unittest.main()
