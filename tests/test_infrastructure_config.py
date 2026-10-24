"""Explicit worker restrictions fail closed when required environment values are absent."""
from pathlib import Path
import sys
import tempfile
import unittest
from tests import test_start as startup
sys.path.insert(0, str(startup.ADWS))
from adw_modules import infrastructure
from adw_modules.data_types import KHConfig


class InfrastructureConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(infrastructure.reset)
        self.session = Path(self.temp.name)

    def configure(self, **settings):
        cfg = KHConfig.model_validate({'execution': {'infrastructure': settings}})
        infrastructure.configure(cfg, 'config-proof')
        return cfg

    def test_missing_or_empty_worker_credential_refuses_before_any_capability_probe(self):
        cfg = self.configure(worker_env={'DATABASE_URL': '${LIMITED_DATABASE_URL}'})
        for env in ({}, {'LIMITED_DATABASE_URL': ''}, {'LIMITED_DATABASE_URL': '   '}):
            with self.subTest(env=env), self.assertRaisesRegex(RuntimeError, 'LIMITED_DATABASE_URL'):
                infrastructure.run_capability_checks(cfg, env, self.session)
        self.assertFalse((self.session / 'infrastructure_checks.json').exists())

    def test_unset_probe_reference_does_not_silently_become_empty_argv(self):
        cfg = self.configure(capability_checks=[{'name': 'limited-role',
            'argv': [sys.executable, '-c', 'print("f")', '${WORKER_TARGET}'], 'expect_stdout': 'f'}])
        with self.assertRaisesRegex(RuntimeError, 'WORKER_TARGET'):
            infrastructure.run_capability_checks(cfg, {}, self.session)
        self.assertFalse((self.session / 'infrastructure_checks.json').exists())

    def test_explicit_worker_environment_preserves_operator_credentials(self):
        cfg = self.configure(withhold_env=['ADMIN_DATABASE_URL'],
                             worker_env={'DATABASE_URL': '${LIMITED_DATABASE_URL}'})
        operator = {'ADMIN_DATABASE_URL': 'admin-only', 'LIMITED_DATABASE_URL': 'limited-worker', 'PATH': 'toolchain'}
        before = dict(operator)
        self.assertEqual(infrastructure.run_capability_checks(cfg, operator, self.session), [])
        worker = infrastructure.apply_worker_env(dict(operator))
        self.assertEqual(worker['DATABASE_URL'], 'limited-worker')
        self.assertNotIn('ADMIN_DATABASE_URL', worker)
        self.assertEqual(worker['PATH'], 'toolchain')
        self.assertEqual(operator, before)

    def test_config_command_inspection_unwraps_capture_without_changing_worker_settings(self):
        self.configure(worker_env={'DATABASE_URL':'original-worker'},
                       forbidden_commands=['original-owner-rule'])
        cfg=KHConfig.model_validate({})
        safe='env DATABASE_URL=unavailable-test python3 adws/kh_exec.py --class test --timeout 30 -- pytest tests/docker -k restart'
        restart='DATABASE_URL=unavailable-test python3 adws/kh_exec.py --class test --timeout 30 -- docker restart database'
        self.assertEqual(infrastructure.inspect_config(safe,cfg),[])
        self.assertIn('docker restart',infrastructure.inspect_config(restart,cfg))
        self.assertEqual(infrastructure.forbidden_patterns(),['original-owner-rule'])
        self.assertEqual(infrastructure.apply_worker_env({})['DATABASE_URL'],'original-worker')

    def test_config_command_inspection_retains_database_and_wrapped_container_prohibitions(self):
        cfg=KHConfig.model_validate({'execution':{'infrastructure':{'deny_executables':['psql']}}})
        base='python3 adws/kh_exec.py --class test --timeout 30 -- '
        for command in (base+"sh -c 'docker restart database'",
                        base+"psql -c 'ALTER SYSTEM SET max_connections = 100'",
                        base+"psql -c 'select 1'"):
            with self.subTest(command=command):
                self.assertTrue(infrastructure.inspect_config(command,cfg))

    def test_declared_test_prefix_does_not_bypass_a_denied_env_executable(self):
        cfg=KHConfig.model_validate({'execution':{'infrastructure':{'deny_executables':['env']}}})
        base='python3 adws/kh_exec.py --class test --timeout 30 -- pytest'
        self.assertTrue(infrastructure.inspect_config('env DATABASE_URL=unavailable-test '+base,cfg))
        self.assertEqual(infrastructure.inspect_config('DATABASE_URL=unavailable-test '+base,cfg),[])
        cfg.execution.infrastructure.deny_executables=['python3']
        self.assertTrue(infrastructure.inspect_config('env DATABASE_URL=unavailable-test '+base,cfg))

    def test_actual_command_inspection_sees_container_actions_inside_prefixed_capture(self):
        self.configure()
        command='env DATABASE_URL=unavailable-test python3 adws/kh_exec.py --class test --timeout 30 -- docker restart database'
        self.assertIn('docker restart',infrastructure.inspect(command))

    def test_nested_absolute_denied_wrappers_are_detected_before_a_test_grant(self):
        import shlex
        for denied,script in (('env','/usr/bin/env DATABASE_URL=fixture python3 -m unittest'),
                              ('sudo','/usr/bin/sudo -n true')):
            cfg=KHConfig.model_validate({'execution':{'infrastructure':{'deny_executables':[denied]}}})
            command=shlex.join(['python3','adws/kh_exec.py','--class','test','--timeout','30','--','sh','-c',script])
            with self.subTest(denied=denied):
                self.assertIn('denied executable: '+denied,infrastructure.inspect_config(command,cfg))
