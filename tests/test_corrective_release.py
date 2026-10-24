"""Regression probes for audited promotion/configuration failures and tracked startup."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_start as startup
sys.path.insert(0, str(startup.ADWS))
import adw_project
from adw_modules import agent_cc, agents, permissions, project, quality
from adw_modules.data_types import KHConfig


class CorrectiveReleaseTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke
    unchanged = startup.StartupTests.unchanged

    def save_config(self):
        self.config.write_text(json.dumps(self.cfg))

    def init_plan(self):
        task = {'id': 'T1', 'title': 'Change application value', 'milestone_id': 'M1',
                'request': 'Implement value 2', 'deliverables': ['Update value'],
                'non_goals': ['Unrelated features'], 'allowed_paths': ['src/app.py'], 'weight': 3}
        project.init_project(self.root, {'schema_version': 1, 'project': {'id': 'P1', 'name': 'Fixture'},
            'milestones': [{'id': 'M1', 'title': 'Implementation'}, {'id': 'M2', 'title': 'Integration'}],
            'tasks': [task, {**task, 'id': 'T2', 'milestone_id': 'M2', 'dependencies': ['T1'], 'weight': 1}]})

    def tracked(self, task='T1', run='tracked', mode='good', extra=()):
        return subprocess.run([sys.executable, str(startup.ADWS/'adw_start.py'), '--task', task,
            '--adw-id', run, *extra], cwd=self.root, env={**self.env, 'KH_FIXTURE_MODE': mode},
            text=True, capture_output=True, timeout=60)

    def test_later_successful_command_mutating_tested_source_cannot_promote(self):
        self.cfg['defaults']['required_quality_checks'] = ['test', 'lint']
        self.cfg['defaults']['quality_checks']['lint'] = [sys.executable, '-c',
            "from pathlib import Path; Path('src/app.py').write_text('value = 3\\n')"]
        self.save_config()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.unchanged()
        logs = list((self.root/'adws/adw_data/sessions').rglob('command.log'))
        self.assertTrue(any('VERIFICATION_SOURCE_CHANGED' in p.read_text() for p in logs))

    def test_generated_build_output_is_not_source_and_owner_cache_is_unchanged(self):
        (self.root/'.gitignore').write_text('dist/\n')
        (self.root/'dist').mkdir()
        (self.root/'dist/result.txt').write_text('owner cache')
        self.cfg['defaults']['required_quality_checks'] = ['test', 'build']
        self.cfg['defaults']['quality_checks']['build'] = [sys.executable, '-c',
            "from pathlib import Path; Path('dist').mkdir(exist_ok=True); Path('dist/result.txt').write_text('candidate build')"]
        self.save_config()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(), 'value = 2\n')
        self.assertEqual((self.root/'dist/result.txt').read_text(), 'owner cache')

    def test_swiftpm_build_output_is_not_a_source_change_by_default(self):
        # `swift build`/`swift test` fill app/.build and app/.swiftpm; with no
        # project generated_paths, every check used to fail VERIFICATION_SOURCE_CHANGED.
        self.assertNotIn('generated_paths', self.cfg['defaults'])
        self.cfg['defaults']['required_quality_checks'] = ['test', 'build']
        self.cfg['defaults']['quality_checks']['build'] = [sys.executable, '-c',
            "from pathlib import Path\n"
            "for d in ('app/.build/debug', 'app/.swiftpm/xcode', 'android/.gradle/8.0', 'mobile/.dart_tool/pkg'):\n"
            "    Path(d).mkdir(parents=True, exist_ok=True); (Path(d) / 'out.bin').write_text('artifact')"]
        self.save_config()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root/'src/app.py').read_text(), 'value = 2\n')
        for name in ('app/.build', 'app/.swiftpm', 'android/.gradle', 'mobile/.dart_tool'):
            self.assertFalse((self.root/name).exists(), name + ' must not be promoted as source')

    def test_plain_build_and_target_folders_are_still_source_by_default(self):
        defaults = KHConfig().defaults.generated_paths
        for name in ('**/.build/', '**/.swiftpm/', '**/.gradle/', '**/.dart_tool/'):
            self.assertIn(name, defaults)
        self.assertNotIn('**/build/', defaults)
        self.assertNotIn('**/target/', defaults)

    def test_large_declared_cache_not_held_in_source_snapshot_but_tracked_files_are(self):
        cache = self.root/'node_modules/package'
        cache.mkdir(parents=True)
        (cache/'large.js').write_bytes(b'x' * 65536)
        cfg = agents.load_config(str(self.config))
        cfg.defaults.snapshot_max_bytes = 16384
        run = SimpleNamespace(repo_root=self.root, cfg=cfg)
        snap = permissions.snapshot(run)
        self.assertNotIn('node_modules/package/large.js', snap)
        subprocess.run(['git', 'add', '-f', 'node_modules/package/large.js'], cwd=self.root, check=True)
        with self.assertRaisesRegex(permissions.PermissionBreach, 'Snapshot exceeds'):
            permissions.snapshot(run)

    def test_dependency_discovery_excludes_parallel_lane_and_job_state(self):
        run = SimpleNamespace(repo_root=self.root, cfg=agents.load_config(str(self.config)))
        owner = self.root/'node_modules/package/value.txt'
        private = [self.root/'.kh/parallel/run/lanes/T1/node_modules/package/value.txt',
                   self.root/'.kh/parallel/run/jobs/T1/tmp/kh-agent-x/workspace/node_modules/package/value.txt']
        for path in [owner, *private]:
            path.parent.mkdir(parents=True)
            path.write_text('1')
        self.assertEqual(permissions.generated_roots(run, dependencies_only=True), ['node_modules'])
        before = permissions.dependency_fingerprint(run)
        for path in private:
            path.write_text('private worker changed')
        self.assertEqual(permissions.dependency_fingerprint(run), before)
        owner.write_text('owner dependency changed')
        self.assertNotEqual(permissions.dependency_fingerprint(run), before)

    def test_ignored_owner_files_are_still_source(self):
        (self.root/'.gitignore').write_text('private.txt\n')
        cfg = agents.load_config(str(self.config))
        snap = permissions.snapshot(SimpleNamespace(repo_root=self.root, cfg=cfg))
        self.assertIn('private.txt', snap)

    def test_verification_cannot_mutate_dependency_inputs(self):
        cache = self.root/'node_modules/package'
        cache.mkdir(parents=True)
        (cache/'value.txt').write_text('1')
        self.cfg['defaults']['required_quality_checks'] = ['lint', 'test']
        self.cfg['defaults']['quality_checks']['lint'] = [sys.executable, '-c',
            "from pathlib import Path; Path('node_modules/package/value.txt').write_text('2')"]
        self.cfg['defaults']['quality_checks']['test'] = [sys.executable, '-c',
            "from pathlib import Path; assert Path('node_modules/package/value.txt').read_text() == '2'"]
        self.save_config()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.unchanged()
        self.assertEqual((cache/'value.txt').read_text(), '1')
        logs = list((self.root/'adws/adw_data/sessions').rglob('command.log'))
        self.assertTrue(any('VERIFICATION_SOURCE_CHANGED' in p.read_text() for p in logs))

    def test_cache_symlink_cannot_reference_outside_owner(self):
        cache = self.root/'node_modules'
        cache.mkdir()
        outside = self.base/'external-package.js'
        outside.write_text('outside')
        (cache/'link.js').symlink_to(outside)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlink escapes project', result.stdout + result.stderr)
        self.unchanged()
        self.assertEqual(outside.read_text(), 'outside')

    def test_unknown_keys_are_rejected_at_all_configuration_levels(self):
        mutations = [lambda c: c.update(budgets={}),
                     lambda c: c['defaults'].update(max_token_budget=100),
                     lambda c: c['agents'][0].update(writess=[]),
                     lambda c: c['agents'][0]['prompt_engineering'].update(sytem='x'),
                     lambda c: c.update(observability={'enabledd': False})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                config = json.loads(json.dumps(self.cfg))
                mutate(config)
                with self.assertRaises(ValueError):
                    KHConfig.model_validate(config)

    def test_tracked_acceptance_records_handoffs_receipt_and_weighted_remaining_work(self):
        self.init_plan()
        result = self.tracked()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snapshot = project.status(self.root)
        self.assertEqual(snapshot['totals']['completed'], 1)
        self.assertEqual(snapshot['totals']['completion_percent'], 75)
        self.assertEqual(snapshot['next_tasks'], ['T2'])
        self.assertEqual(snapshot['milestones'][0]['status'], 'completed')
        task = snapshot['tasks'][0]
        self.assertEqual(task['changed_files'], ['src/app.py'])
        self.assertEqual(task['run_id'], 'tracked')
        self.assertEqual(len(task['receipt']['handoffs']), 4)
        self.assertEqual(task['receipt']['handoffs'][1]['phase'], 'plan_review_1')
        self.assertTrue(all((self.root/p).is_file() for p in task['evidence']))
        report = (self.root/'.kh/task-reports/T1.md').read_text()
        self.assertIn('builder', report)
        self.assertIn('review', report)
        self.assertIn('75', (self.root/'.kh/PROJECT_STATUS.md').read_text())
        self.assertEqual(json.loads((self.root/'.kh/project-status.json').read_text())['totals'], snapshot['totals'])

    def test_pause_during_checks_finishes_current_task_without_owner_drift(self):
        self.init_plan()
        original = quality.run_quality
        requested = False
        def check_and_pause(view):
            nonlocal requested
            if not requested:
                requested = True
                adw_project.request_pause_after_current(self.root)
            return original(view)
        with patch.dict(os.environ, self.env), \
             patch.object(agent_cc, 'CC_PATH', str(self.cli)), \
             patch.object(quality, 'run_quality', side_effect=check_and_pause):
            result = adw_project.run_continuous(self.root, notify=lambda _event: None)
        self.assertTrue(requested)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual([entry['task_id'] for entry in result['attempted']], ['T1'])
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'completed')
        self.assertEqual(project.load_task(self.root, 'T2')['status'], 'pending')

    def test_unfinished_dependency_refuses_worker_dispatch(self):
        self.init_plan()
        result = self.tracked(task='T2')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('dependenc', (result.stdout + result.stderr).lower())
        self.unchanged()
        self.assertFalse((self.root/'adws/adw_data/sessions/tracked/agent_map.json').exists())
        self.assertEqual(project.status(self.root)['totals']['completed'], 0)

    def test_failed_tracked_task_is_blocked_and_retry_with_new_run_can_complete(self):
        self.init_plan()
        result = self.tracked(mode='semantic')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.unchanged()
        snapshot = project.status(self.root)
        self.assertEqual(snapshot['tasks'][0]['status'], 'blocked')
        self.assertTrue(snapshot['tasks'][0]['blocker'])
        self.assertEqual(snapshot['totals']['completion_percent'], 0)
        self.assertFalse(project.receipt_path(self.root, 'T1', 'tracked').exists())
        retry = self.tracked(run='retry')
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(project.status(self.root)['tasks'][0]['status'], 'completed')

    def test_tracked_dry_run_does_not_start_or_change_ledger(self):
        self.init_plan()
        before = (self.root/'.kh/project.db').read_bytes()
        result = self.tracked(extra=['--dry-run'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, (self.root/'.kh/project.db').read_bytes())
        self.assertFalse((self.root/'adws/adw_data/sessions').exists())

    def test_conflicting_scope_cannot_replace_project_plan(self):
        self.init_plan()
        result = self.tracked(extra=['--scope-file', str(self.scope)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('do not combine', result.stderr)
        self.assertEqual(project.status(self.root)['tasks'][0]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
