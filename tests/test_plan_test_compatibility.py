"""Native test feasibility is checked while plans can still be corrected."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import command_grants, planning, quality, runtime_compatibility, scope
from adw_modules.command_limits import command_limit
from adw_modules.data_types import AgentConfig, PromptEngineering
from tests import test_planning_review as planning_fixtures
from tests import test_reviewed_plan_revision as revision_fixtures


COMMAND = 'python3 -B -m unittest'


class PlanTestCompatibilityTests(unittest.TestCase):
    plan = planning_fixtures.PlanningTests.plan
    execute = planning_fixtures.PlanningTests.execute
    review = staticmethod(planning_fixtures.PlanningTests.review)

    def setUp(self):
        planning_fixtures.PlanningTests.setUp(self)
        self.builder = AgentConfig(name='builder', coding_agent='claude_code',
            tools=['read', 'write'], writes=['src/**'],
            prompt_engineering=PromptEngineering(system='x', user='y'))
        self.run.cfg.agents.append(self.builder)

    def validate(self, commands):
        plan = self.plan().model_copy(update={'required_test_commands': commands})
        return planning.validate_plan(plan, self.contract, self.run.cfg.defaults.required_quality_checks,
            self.run.cfg.defaults.required_test_commands, run=self.run)

    def exact_command(self):
        self.run.cfg.defaults.quality_checks = {'test': ['python3', '-B', '-m', 'unittest']}
        return command_grants.capture_commands(self.run.cfg.defaults.quality_checks,
                                               command_limit(self.builder.timeout_seconds))[0]

    def test_infeasible_planner_addition_is_replanned_without_becoming_permanent(self):
        calls = []
        def callback(run, phase, call):
            brief = json.loads(call.prompt)
            calls.append((call.output_type.__name__, brief['version']))
            if call.output_type is planning.ReviewedPlanOutput:
                if brief['version'] == 2:
                    self.assertNotIn(COMMAND, brief['retained_test_commands'])
                    self.assertEqual(brief['previous_plan']['required_test_commands'], [COMMAND])
                commands = [COMMAND] if brief['version'] == 1 else []
                return self.plan().model_copy(update={'required_test_commands': commands})
            return self.review(call, resolved_finding_ids=(
                ['CONTROLLER-VALIDATION'] if brief['version'] > 1 else []))
        plan, approval = self.execute(callback)
        self.assertEqual(approval.version, 2,
                         'An unsupported native command must be rejected before independent plan approval')
        self.assertEqual(plan.required_test_commands, [])
        self.assertEqual(calls, [('ReviewedPlanOutput', 1), ('ReviewedPlanOutput', 2), ('PlanReviewOutput', 2)])
        validation = json.loads((self.run.session_dir / 'planning/validation-1.json').read_text())
        self.assertIn(COMMAND, validation['summary'])
        self.assertIn('incompatible with builder', validation['summary'])

    def test_replanning_removes_only_invalid_addition_and_keeps_owner_command(self):
        exact = self.exact_command()
        self.builder.tools = ['read', 'write', 'bash']
        self.run.cfg.defaults.required_test_commands = [exact]
        calls = []
        def callback(run, phase, call):
            brief = json.loads(call.prompt)
            calls.append((call.output_type.__name__, brief['version']))
            if call.output_type is planning.ReviewedPlanOutput:
                self.assertEqual(brief['retained_test_commands'], [exact])
                return self.plan().model_copy(update={'required_test_commands':
                    [exact, 'docker restart fixture-database'] if brief['version'] == 1 else [exact]})
            return self.review(call, resolved_finding_ids=['CONTROLLER-VALIDATION'])
        plan, approval = self.execute(callback)
        self.assertEqual(approval.version, 2)
        self.assertEqual(plan.required_test_commands, [exact])
        self.assertEqual(self.run.cfg.defaults.required_test_commands, [exact])
        self.assertEqual(calls, [('ReviewedPlanOutput', 1), ('ReviewedPlanOutput', 2), ('PlanReviewOutput', 2)])

    def test_direct_validation_rejects_missing_shell_with_named_command(self):
        with self.assertRaisesRegex(planning.PlanningBlocked, 'incompatible with builder') as caught:
            self.validate([COMMAND])
        self.assertIn(COMMAND, str(caught.exception))
        self.assertIn('shell', str(caught.exception).lower())


    def test_both_exact_declared_database_override_forms_pass_early_validation(self):
        base = self.exact_command()
        self.builder.tools = ['read', 'write', 'bash']
        self.run.cfg.defaults.test_environment_overrides = {'DATABASE_URL': ['unavailable-test-fixture']}
        commands = ['DATABASE_URL=unavailable-test-fixture ' + base,
                    'env DATABASE_URL=unavailable-test-fixture ' + base]
        before = self.run.cfg.model_dump()
        with patch('subprocess.Popen', side_effect=AssertionError('early validation must not execute tests')):
            for command in commands:
                with self.subTest(command=command):
                    self.validate([command])
            contract = planning._native_test_contract(self.run)
        self.assertTrue(set(commands) <= set(contract['capture_commands']))
        self.assertEqual(contract['builder_tools'], self.builder.tools)
        self.assertEqual(contract['builder_status'], 'compatible')
        self.assertEqual(contract['native_permissions'], 'unverified')
        self.assertEqual(self.run.cfg.model_dump(), before)

    def test_exact_capture_does_not_authorize_container_or_database_reconfiguration(self):
        self.builder.tools = ['read', 'write', 'bash']
        for argv in (['docker', 'restart', 'fixture-database'],
                     ['psql', '-c', 'ALTER SYSTEM SET work_mem = 999']):
            with self.subTest(argv=argv):
                self.run.cfg.defaults.quality_checks = {'test': argv}
                exact = command_grants.capture_commands(self.run.cfg.defaults.quality_checks,
                    command_limit(self.builder.timeout_seconds))[0]
                before = self.run.cfg.model_dump()
                with patch('subprocess.Popen', side_effect=AssertionError('forbidden command must never execute')):
                    with self.assertRaisesRegex(planning.PlanningBlocked, 'incompatible with builder') as caught:
                        self.validate([exact])
                self.assertIn('infrastructure boundary', str(caught.exception))
                self.assertEqual(self.run.cfg.model_dump(), before)

    def test_owner_required_command_cannot_be_dropped(self):
        self.run.cfg.defaults.required_test_commands = [COMMAND]
        with self.assertRaisesRegex(planning.PlanningBlocked, 'omitted required native test'):
            self.validate([])
        self.assertEqual(self.run.cfg.defaults.required_test_commands, [COMMAND])

    def test_replanning_cannot_remove_an_incompatible_owner_requirement(self):
        self.run.cfg.defaults.required_test_commands = [COMMAND]
        calls = []
        def callback(run, phase, call):
            brief = json.loads(call.prompt)
            calls.append((call.output_type.__name__, brief['version']))
            if call.output_type is planning.ReviewedPlanOutput:
                return self.plan().model_copy(update={
                    'required_test_commands': [COMMAND] if brief['version'] == 1 else []})
            return self.review(call)
        with self.assertRaisesRegex(planning.PlanningBlocked, 'omitted required native test'):
            self.execute(callback)
        self.assertEqual(calls, [('ReviewedPlanOutput', 1), ('ReviewedPlanOutput', 2)])
        self.assertEqual(self.run.cfg.defaults.required_test_commands, [COMMAND])
        self.assertFalse((self.run.session_dir / 'planning/approval.json').exists())

    def test_infrastructure_denial_and_configuration_are_unchanged(self):
        self.builder.coding_agent = 'claude_code'
        self.builder.tools = ['read', 'write', 'bash']
        before = self.run.cfg.model_dump()
        with self.assertRaisesRegex(planning.PlanningBlocked, 'incompatible with builder') as caught:
            self.validate(['docker restart fixture-database'])
        self.assertIn('docker restart fixture-database', str(caught.exception))
        self.assertEqual(self.run.cfg.model_dump(), before)

    def test_structural_and_plan_checks_never_run_processes_or_host_probes(self):
        self.builder.coding_agent = 'claude_code'
        self.builder.tools = ['read', 'write', 'bash']
        self.builder.required_capabilities = ['swift']
        with patch('subprocess.Popen', side_effect=AssertionError('planning must not execute a process')), \
             patch.object(runtime_compatibility, 'check', side_effect=AssertionError('dispatch check is not planning')):
            report = runtime_compatibility.structural(self.builder, self.run.cfg,
                required_test_commands=[COMMAND], run_id=self.run.adw_id)
            self.validate([COMMAND])
        self.assertEqual(report['status'], 'compatible')
        self.assertEqual(report['probes'], [])
        self.assertIn(COMMAND, report['required_test_commands'])
        self.assertTrue(any(item['capability'] == 'native_permissions' for item in report['unverified']))

    def test_command_bearing_plan_requires_a_configured_builder(self):
        self.run.cfg.agents.remove(self.builder)
        with self.assertRaisesRegex(planning.PlanningBlocked, 'builder'):
            self.validate([COMMAND])
        self.validate([])

    def test_final_structure_gate_rejects_incompatible_native_commands(self):
        report = planning.structure_gate(self.contract, ['test'])(
            self.plan().model_copy(update={'required_test_commands': [COMMAND]}), self.run)
        self.assertTrue(report.violations)
        self.assertIn('incompatible with builder', '\n'.join(report.violations))
        self.assertIn(COMMAND, '\n'.join(report.violations))


class SavedPlanTestCompatibilityTests(unittest.TestCase):
    review = staticmethod(revision_fixtures.ReviewedPlanRevisionTests.review)
    revise = revision_fixtures.ReviewedPlanRevisionTests.revise

    def plan(self, *, revised=False, **updates):
        updates.setdefault('required_test_commands', [COMMAND])
        return revision_fixtures.ReviewedPlanRevisionTests.plan(self, revised=revised, **updates)

    def setUp(self):
        revision_fixtures.ReviewedPlanRevisionTests.setUp(self)
        self.builder = next(agent for agent in self.run.cfg.agents if agent.name == 'builder')

    def cached_plan(self):
        self.source.write_text('value = 1\n')
        directory = self.root / 'adws/adw_data/sessions/next'
        resumed = SimpleNamespace(**{**vars(self.run), 'adw_id': 'next', 'session_dir': directory,
            'scope_contract': self.contract, 'handoff_root': directory / 'context_handoff',
            'expected_inputs': {}, 'console': SimpleNamespace(note=lambda message: None)})
        scope.capture_contract(resumed, self.contract)
        reused = planning.reuse_approved_plan(resumed, self.contract.original_request, self.contract,
            self.packet, baseline=quality.source_fingerprint(resumed), roles=planning._roles(resumed), max_repairs=1)
        return reused, directory

    def test_cached_approval_remains_reusable_with_compatible_builder(self):
        reused, directory = self.cached_plan()
        self.assertIsNotNone(reused, 'The fixture must reach the actual cache validation path')
        self.assertTrue((directory / 'planning/approval.json').exists())

    def test_cached_approval_is_not_reused_after_builder_loses_shell(self):
        self.builder.tools = ['read', 'write']
        reused, directory = self.cached_plan()
        self.assertIsNone(reused)
        self.assertFalse((directory / 'planning/approval.json').exists())

    def test_execution_approval_rechecks_current_builder_capabilities(self):
        self.builder.tools = ['read', 'write']
        self.source.write_text('value = 1\n')
        with self.assertRaisesRegex(planning.PlanningBlocked, 'incompatible with builder'):
            planning.assert_plan_approval(self.run, self.old_plan, self.old_approval, self.contract, self.packet)

    def test_old_plan_is_rechecked_before_dispatching_a_revision(self):
        self.builder.tools = ['read', 'write']
        pointer = planning.approval_path(self.run).read_bytes()
        prior_phases = list(self.phases)
        with self.assertRaisesRegex(planning.PlanningBlocked, 'incompatible with builder'):
            self.revise()
        self.assertEqual(self.phases, prior_phases)
        self.assertEqual(planning.approval_path(self.run).read_bytes(), pointer)

    def test_incompatible_added_revision_command_never_reaches_reviewer(self):
        pointer = planning.approval_path(self.run).read_bytes()
        prior_count = len(self.phases)
        proposal = self.plan(revised=True, required_test_commands=[COMMAND, 'docker restart fixture-database'])
        with self.assertRaisesRegex(planning.PlanningBlocked, 'incompatible with builder'):
            self.revise(proposal=proposal)
        self.assertEqual(self.phases[prior_count:], ['replan_2'])
        self.assertEqual(planning.approval_path(self.run).read_bytes(), pointer)
        self.assertFalse((self.run.session_dir / 'planning/approval-2.json').exists())


if __name__ == '__main__':
    unittest.main()
