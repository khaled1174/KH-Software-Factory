"""Native test obligations survive independently reviewed plan corrections."""
import json
import unittest
from types import SimpleNamespace
from tests import test_planning_review, test_reviewed_plan_revision
from adw_modules import planning, quality, scope
from adw_modules.data_types import AgentConfig, PromptEngineering

COMMAND='python3 adws/kh_exec.py --class test --timeout 450 -- python3 -B -m unittest'


class NativeTestAmendmentTests(unittest.TestCase):
    setUp=test_reviewed_plan_revision.ReviewedPlanRevisionTests.setUp
    review=staticmethod(test_reviewed_plan_revision.ReviewedPlanRevisionTests.review)
    revise=test_reviewed_plan_revision.ReviewedPlanRevisionTests.revise

    def plan(self,*,revised=False,**updates):
        updates.setdefault('required_test_commands',[COMMAND] if not revised else [])
        return test_reviewed_plan_revision.ReviewedPlanRevisionTests.plan(self,revised=revised,**updates)

    def test_amendment_cannot_drop_a_required_native_test(self):
        approval=self.run.session_dir/'planning/approval.json'
        before=approval.read_bytes()
        with self.assertRaisesRegex(planning.PlanningBlocked,'native test'):
            self.revise()
        self.assertEqual(approval.read_bytes(),before)
        self.assertFalse((self.run.session_dir/'planning/approval-2.json').exists())


class NativeTestCorrectionTests(unittest.TestCase):
    def setUp(self):
        test_planning_review.PlanningTests.setUp(self)
        # This fixture tests retention of an executable obligation, not an
        # unsupported planner addition that should be corrected before review.
        self.run.cfg.agents.append(AgentConfig(name='builder', coding_agent='claude_code',
            tools=['Read', 'Write', 'Bash'],
            prompt_engineering=PromptEngineering(system='x', user='y')))

    plan=test_planning_review.PlanningTests.plan
    execute=test_planning_review.PlanningTests.execute
    review=staticmethod(test_planning_review.PlanningTests.review)

    def test_correction_cannot_drop_a_required_native_test(self):
        calls=[]
        def callback(run,phase,call):
            calls.append(call)
            brief=json.loads(call.prompt)
            if call.output_type is planning.ReviewedPlanOutput:
                result=self.plan('Implement widget with edge case' if brief['version']>1 else 'Implement widget')
                result=result.model_copy(update={'required_test_commands':[COMMAND] if brief['version']==1 else []})
                if brief['version']>1:
                    # The callback substitutes agents.execute, including its mandatory gate.
                    structure=[gate for gate in call.gates if gate.__name__=='plan_structure']
                    self.assertEqual(len(structure),1,'Final revisions need the mandatory structural gate')
                    report=structure[0](result,run)
                    if not report.passed:
                        raise planning.agents.GateFailure('\n'.join(report.violations))
                return result
            if brief['version']==1:
                return self.review(call,decision='revise',findings=[planning.PlanFinding(id='F1',severity='major',
                    step_id='S1',evidence='Missing edge case',correction='Cover empty input')])
            return self.review(call,resolved_finding_ids=['F1'])
        with self.assertRaisesRegex(planning.PlanningBlocked,'native test'):
            self.execute(callback)
        self.assertFalse((self.run.session_dir/'planning/approval.json').exists())
        self.assertFalse(any(call.output_type is planning.PlanReviewOutput and json.loads(call.prompt)['version']==2
                             for call in calls),'incomplete revision must stop before independent approval')


class NativeTestOwnerChangeTests(unittest.TestCase):
    setUp=test_reviewed_plan_revision.ReviewedPlanRevisionTests.setUp
    plan=test_reviewed_plan_revision.ReviewedPlanRevisionTests.plan
    review=staticmethod(test_reviewed_plan_revision.ReviewedPlanRevisionTests.review)
    revise=test_reviewed_plan_revision.ReviewedPlanRevisionTests.revise

    def test_cached_plan_omitting_new_owner_test_is_a_cache_miss(self):
        self.run.cfg.defaults.required_test_commands=[COMMAND]
        self.source.write_text('value = 1\n')
        directory=self.root/'adws/adw_data/sessions/next'
        resumed=SimpleNamespace(**{**vars(self.run),'adw_id':'next','session_dir':directory,
            'scope_contract':self.contract,'handoff_root':directory/'context_handoff',
            'expected_inputs':{},'console':SimpleNamespace(note=lambda message:None)})
        scope.capture_contract(resumed,self.contract)
        reused=planning.reuse_approved_plan(resumed,self.contract.original_request,self.contract,self.packet,
            baseline=quality.source_fingerprint(resumed),roles=planning._roles(resumed),max_repairs=1)
        self.assertIsNone(reused,'new owner test obligations require a fresh reviewed plan')
        self.assertFalse((directory/'planning/approval.json').exists())

    def test_execution_approval_requires_current_owner_tests(self):
        self.run.cfg.defaults.required_test_commands=[COMMAND]
        self.source.write_text('value = 1\n')
        with self.assertRaisesRegex(planning.PlanningBlocked,'native test'):
            planning.assert_plan_approval(self.run,self.old_plan,self.old_approval,self.contract,self.packet)

    def test_amendment_rejects_prior_plan_omitting_current_owner_test(self):
        self.run.cfg.defaults.required_test_commands=[COMMAND]
        pointer=self.run.session_dir/'planning/approval.json'
        before=pointer.read_bytes()
        with self.assertRaisesRegex(planning.PlanningBlocked,'native test'):
            self.revise(proposal=self.plan(revised=True,required_test_commands=[COMMAND]))
        self.assertEqual(pointer.read_bytes(),before)


if __name__=='__main__':unittest.main()
