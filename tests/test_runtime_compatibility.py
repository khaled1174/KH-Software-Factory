"""Runtime requirements are checked before providers, without executing retired host probes."""
import contextlib
import io
import json
import runpy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tests import test_core

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import agent_cc, infrastructure, runtime_compatibility
from adw_modules.data_types import AgentConfig, ConfigDefaults, KHConfig, PlanOutput, AgentCall, BuildOutput, PhaseParams


class RuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='kh-runtime-check-')
        self.addCleanup(temp.cleanup)
        self.addCleanup(infrastructure.reset)
        self.base = Path(temp.name).resolve()
        self.work = self.base / 'worker'; self.work.mkdir()
        self.sessions = self.base / 'sessions'; self.sessions.mkdir()
        self.bin = self.base / 'bin'; self.bin.mkdir()
        self.cfg = KHConfig.model_validate({})
        self.run = SimpleNamespace(repo_root=self.work, session_dir=self.sessions,
                                   cfg=self.cfg, active_phase_id='phase-check')

    def agent(self, **changes):
        fields = {'name': 'builder', 'coding_agent': 'claude_code', 'tools': ['Read', 'Write', 'Bash'],
                  'prompt_engineering': {'system': 'system.md', 'user': 'user.md'}}
        return AgentConfig.model_validate({**fields, **changes})

    def executable(self, name, code):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + code + '\n')
        path.chmod(0o700)
        return path

    def failed(self, agent):
        with self.assertRaisesRegex(RuntimeError, '^RUNTIME_INCOMPATIBLE') as caught:
            runtime_compatibility.check(self.run, agent)
        report = caught.exception.report
        self.assertEqual(report['status'], 'incompatible')
        self.assertTrue(report['blocked'])
        self.assertTrue(Path(report['artifact']).is_file())
        return report

    def test_structural_check_has_no_process_or_provider_side_effects(self):
        agent = self.agent(required_capabilities=['shell'])
        with patch('subprocess.Popen', side_effect=AssertionError('must not execute')):
            report = runtime_compatibility.structural(agent, self.cfg)
        self.assertEqual(report['status'], 'compatible')
        self.assertEqual(report['probes'], [])
        self.assertFalse(list(self.sessions.rglob('*')))

    def test_retired_host_probes_and_legacy_capabilities_launch_no_processes(self):
        self.executable('swift', 'print("Swift fixture")')
        agent = self.agent(required_capabilities=['shell', 'swift', 'database'],
                           capability_probes=[{'capability': 'database',
                               'argv': [sys.executable, '-c', 'print("legacy ready")'],
                               'expect_stdout': 'legacy ready'}])
        with patch.dict(os.environ, {'PATH': str(self.bin)}), \
             patch('subprocess.Popen', wraps=subprocess.Popen) as launched:
            report = runtime_compatibility.check(self.run, agent)
        self.assertEqual(0, launched.call_count, 'Retired host probes must not launch subprocesses')
        self.assertEqual(report['status'], 'compatible')
        self.assertEqual(report['probes'], [])
        self.assertTrue(any(item['capability'] == 'native_permissions' for item in report['unverified']))

    def test_codex_read_only_source_mismatch_is_proven_before_a_probe(self):
        marker = self.work / 'probe-executed'
        probe = {'capability': 'shell', 'argv': [sys.executable, '-c',
                 'from pathlib import Path; Path("probe-executed").touch(); print("ready")'],
                 'expect_stdout': 'ready'}
        agent = self.agent(coding_agent='delegate_codex', delegate_sandbox='read-only', tools=None,
                           required_capabilities=['source_write', 'shell'], capability_probes=[probe])
        self.failed(agent)
        self.assertFalse(marker.exists())

    def test_known_adapter_tool_and_extension_constraints_are_structural(self):
        for changes in ({'coding_agent': 'delegate_codex', 'tools': ['Read']},
                        {'coding_agent': 'delegate_codex', 'tools': None, 'harness_engineering': ['extension.ts']}):
            with self.subTest(changes=changes):
                self.assertEqual(runtime_compatibility.structural(self.agent(**changes), self.cfg)['status'], 'incompatible')

    def test_read_only_codex_cannot_deliver_mandatory_phase_evidence(self):
        self.run.context_handoff_dir = self.work / 'phase-evidence'
        self.run.context_handoff_dir.mkdir()
        agent = self.agent(coding_agent='delegate_codex', delegate_sandbox='read-only', tools=None, writes=[])
        report = self.failed(agent)
        self.assertTrue(any(item['capability'] == 'phase_evidence' for item in report['blocked']))

    def test_no_owner_requirements_preserves_host_quality_for_file_only_roles(self):
        agent = self.agent(coding_agent='claude_code', tools=['read', 'write', 'edit'])
        report = runtime_compatibility.check(self.run, agent)
        self.assertEqual(report['status'], 'compatible')
        self.assertEqual(report['probes'], [])
        self.assertTrue(report['unverified'])


    def test_required_shell_swift_and_database_need_a_native_shell_tool(self):
        for capability in ('shell', 'swift', 'database'):
            with self.subTest(capability=capability):
                self.failed(self.agent(tools=['Read', 'Write'], required_capabilities=[capability]))


    def test_probe_config_is_typed_and_cannot_request_permission_elevation(self):
        for changes in ({'required_capabilities': ['unknown']},
                        {'capability_probes': [{'capability': 'database', 'argv': [], 'expect_stdout': 'ok'}]},
                        {'capability_probes': [{'capability': 'database', 'argv': ['x'], 'expect_stdout': 'ok',
                                               'read_only': False}]},
                        {'capability_probes': [{'capability': 'database', 'argv': ['x'], 'expect_stdout': 'ok',
                                               'approval': 'auto'}]},
                        {'capability_probes': [{'capability': 'database', 'argv': ['x'], 'expect_stdout': 'ok',
                                               'timeout_seconds': 0}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.agent(**changes)

    def test_test_command_declarations_are_literal_requirements_without_grants(self):
        command = 'python3 adws/kh_exec.py --class test --timeout 450 -- swift test --filter SpecificCase'
        self.assertEqual(ConfigDefaults(required_test_commands=[command]).required_test_commands, [command])
        self.assertEqual(PlanOutput(status='success', required_test_commands=[command]).required_test_commands, [command])
        for invalid in ('', ' ', 'swift test\nother', 'swift test\r', 'swift\x00test'):
            for constructor in (lambda: ConfigDefaults(required_test_commands=[invalid]),
                                lambda: PlanOutput(status='success', required_test_commands=[invalid])):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    constructor()

    def test_test_environment_overrides_accept_only_owner_literal_database_values(self):
        values = {'DATABASE_URL': ['postgresql://localhost/kh_case', 'postgresql://localhost/kh_case']}
        self.assertEqual(ConfigDefaults(test_environment_overrides=values).test_environment_overrides,
                         {'DATABASE_URL': ['postgresql://localhost/kh_case']})
        for invalid in ({'ADMIN_URL': ['value']}, {'DATABASE_URL': 'value'},
                        {'DATABASE_URL': ['$(read-secrets)']}, {'TEST_DATABASE_URL': ['value\nnext']}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ConfigDefaults(test_environment_overrides=invalid)

    def test_static_and_plan_test_requirements_need_bash_only_for_the_builder(self):
        self.cfg.defaults.required_test_commands = ['swift test --filter SpecificCase']
        agent = self.agent(tools=['Read', 'Write'])
        report = self.failed(agent)
        self.assertTrue(any(item['capability'] == 'required_test_command' for item in report['blocked']))
        reviewer = agent.model_copy(update={'name': 'reviewer', 'writes': []})
        report = runtime_compatibility.check(self.run, reviewer, required_test_commands=['swift test'])
        self.assertEqual(report['status'], 'compatible')
        self.assertEqual(report['native_test_commands'], [])

    def test_approved_plan_test_without_shell_stops_before_any_probe(self):
        agent = self.agent(tools=['read', 'write'],
            capability_probes=[{'capability': 'shell', 'argv': [sys.executable, '-c', 'print("ready")'],
                                'expect_stdout': 'ready'}])
        self.cfg.defaults.quality_checks = {'test': ['swift', 'test']}
        with patch('subprocess.Popen', side_effect=AssertionError('must not execute')):
            with self.assertRaisesRegex(RuntimeError, 'RUNTIME_INCOMPATIBLE') as caught:
                runtime_compatibility.check(self.run, agent, required_test_commands=['swift test --filter SpecificCase'])
        required_rows = [row for row in caught.exception.report['native_test_commands'] if row['required']]
        self.assertEqual(len(required_rows), 1)
        self.assertFalse(required_rows[0]['exact_grant'])

    def test_exact_test_capture_and_declared_db_prefixes_pass_preflight_without_execution(self):
        base = 'python3 adws/kh_exec.py --class test --timeout 450 -- swift test --filter SpecificCase'
        self.cfg.defaults.quality_checks = {'test': ['swift', 'test', '--filter', 'SpecificCase']}
        self.cfg.defaults.test_environment_overrides = {'DATABASE_URL': ['postgresql://localhost/kh_case']}
        agent = self.agent(coding_agent='claude_code', tools=['read', 'write', 'bash'])
        commands = [base, 'DATABASE_URL=postgresql://localhost/kh_case ' + base,
                    'env DATABASE_URL=postgresql://localhost/kh_case ' + base]
        with patch('subprocess.Popen', side_effect=AssertionError('no commands run')):
            report = runtime_compatibility.check(self.run, agent, required_test_commands=commands)
        self.assertEqual(report['status'], 'compatible')
        self.assertEqual(len(report['native_test_commands']), 3)
        self.assertTrue(all(row['exact_grant'] and row['passed'] for row in report['native_test_commands']))

    def test_required_test_commands_merge_owner_and_approved_plan_without_duplicate_checks(self):
        self.cfg.defaults.required_test_commands = ['swift test']
        agent = self.agent()
        report = runtime_compatibility.check(self.run, agent, required_test_commands=['swift test', 'swift test --filter Focus'])
        self.assertEqual(report['required_test_commands'], ['swift test', 'swift test --filter Focus'])
        self.assertEqual(len(report['native_test_commands']), 2)
        self.assertTrue(all(row['permission_status'] == 'unverified' for row in report['native_test_commands']))

    def test_forbidden_native_quality_operations_are_detected_without_global_infra_mutation(self):
        agent = self.agent(coding_agent='claude_code', tools=['read', 'write', 'bash'])
        prior = infrastructure.forbidden_patterns()
        for argv in (['docker', 'restart', 'shared-db'], ['psql', '-c', 'ALTER SYSTEM SET work_mem = 999']):
            self.cfg.defaults.quality_checks = {'test': argv}
            with self.subTest(argv=argv), patch.object(infrastructure, 'configure', side_effect=AssertionError('no configure')):
                report = self.failed(agent)
            self.assertTrue(any(row['forbidden'] for row in report['native_test_commands']))
            self.assertEqual(infrastructure.forbidden_patterns(), prior)
        file_only = agent.model_copy(update={'tools': ['read', 'write']})
        self.assertEqual(runtime_compatibility.structural(file_only, self.cfg)['status'], 'compatible')

    def test_native_command_checks_respect_actual_run_resource_prefix_and_denied_executables(self):
        self.cfg.execution.infrastructure.resource_prefix = 'kh_'
        self.run.adw_id = 'Current-Run'
        agent = self.agent()
        self.cfg.defaults.quality_checks = {'test': ['psql', '-c', 'DROP SCHEMA kh_current_run_probe']}
        self.assertEqual(runtime_compatibility.check(self.run, agent)['status'], 'compatible')
        self.cfg.defaults.quality_checks = {'test': ['psql', '-c', 'DROP SCHEMA shared_owner']}
        report = self.failed(agent)
        self.assertTrue(report['native_test_commands'][0]['forbidden'])
        self.cfg.execution.infrastructure.deny_executables = ['psql']
        self.cfg.defaults.quality_checks = {'test': ['/usr/local/bin/psql', '-c', 'select 1']}
        report = self.failed(agent)
        self.assertIn('denied executable: psql', report['native_test_commands'][0]['forbidden'])


class DispatchCompatibilityTests(unittest.TestCase):
    tearDown = test_core.CoreTests.tearDown

    def setUp(self):
        test_core.CoreTests.setUp(self)
        # Keep native prerequisites local without starting a provider.
        from adw_modules import agent_cc
        fake = self.root.parent / 'fixture-claude-preflight'
        fake.write_text('#!' + sys.executable + '\nraise SystemExit("Unexpected fixture CLI dispatch")\n')
        fake.chmod(0o700)
        for patcher in (patch.object(agent_cc, 'CC_PATH', str(fake)),
                        patch.dict(os.environ, {'CLAUDE_CODE_PATH': str(fake)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_doctor_reports_known_runtime_mismatch_without_capability_or_provider_calls(self):
        from adw_modules import agent_delegate, routing, runtime_compatibility
        script=test_core.ROOT/'.claude/skills/kh/templates/adws/adw_models.py'
        self.cfg.agents[0].delegate_sandbox='read-only'
        inventory={'profiles':{'fixture':{'builder':[{'coding_agent':'delegate_codex',
                    'model':'owner-fixture','thinking':'high','tools':None}]}},'selected':'fixture'}
        output=io.StringIO()
        with patch.object(sys,'argv',[str(script),'--doctor']),\
             patch('adw_modules.agents.load_config',return_value=self.cfg),\
             patch.object(routing,'inventory',return_value=inventory),\
             patch.object(agent_delegate,'doctor',return_value={'fixture':True}),\
             patch('subprocess.Popen') as probe,\
             patch.object(agent_cc,'run') as provider,contextlib.redirect_stdout(output):
            runpy.run_path(str(script),run_name='__main__')
        report=json.loads(output.getvalue())['runtime_compatibility']['fixture']['builder'][0]
        self.assertEqual(report['status'],'incompatible')
        self.assertIn('source_write',[item['capability'] for item in report['blocked']])
        probe.assert_not_called();provider.assert_not_called()
        self.assertFalse((self.root/'.kh/models.json').exists())


    def test_known_tool_mismatch_blocks_actual_dispatch_and_preserves_source(self):
        from adw_modules import runtime_compatibility
        self.cfg.agents[0].tools=['read']
        self.cfg.agents[0].required_capabilities=['source_write','shell']
        with patch.object(agent_cc,'run') as provider,\
             self.assertRaises(runtime_compatibility.RuntimeCompatibilityError) as rejected:
            with self.run.phase(PhaseParams(name='build',kind='agent',owner='builder',
                                           description='Known incompatible tools',retries=0)) as ph:
                ph.call(AgentCall(output_type=BuildOutput,prompt='implement value 2'))
        provider.assert_not_called()
        self.assertEqual((self.root/'source.py').read_text(),'value = 1\n')
        self.assertIn('source_write',[item['capability'] for item in rejected.exception.report['blocked']])
        self.assertTrue(Path(rejected.exception.report['artifact']).is_file())



if __name__ == '__main__':
    unittest.main()
