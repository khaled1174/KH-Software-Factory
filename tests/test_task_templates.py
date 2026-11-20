"""The installed template generator produces drafts the guarded CLI can read."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_start as startup

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path('.claude/skills/kh/scripts/prepare_task.py')
sys.path.insert(0, str(startup.ADWS))
from adw_modules import intent


class TaskTemplateTests(unittest.TestCase):
    def fixture(self):
        project = startup.StartupTests()
        project.setUp()
        self.addCleanup(project.tearDown)
        return project

    def generate(self, fixture, kind, *options, script=ROOT / SCRIPT, request='Update the demo behavior'):
        return subprocess.run([
            sys.executable, '-B', str(script), kind, '--project', str(fixture.root),
            '--config', str(fixture.config), '--task-id', 'T1', '--title', 'Update demo',
            '--request', request, '--problem', 'The current result is wrong',
            '--outcome', 'The demo returns the expected result', '--allow-path', 'src/app.py', *options,
        ], capture_output=True, text=True, timeout=20)

    def assert_success(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def controller(self, fixture, script, *args):
        return subprocess.run([sys.executable, '-B', str(fixture.root / 'adws' / script), *args],
                              cwd=fixture.root, env=fixture.env, capture_output=True, text=True, timeout=30)

    def test_installed_templates_produce_an_owner_acceptance_and_a_guarded_dry_run(self):
        cases = {
            'bug-fix': ('bug', 'first incorrect source boundary'),
            'interface': ('feature', 'Verify the user journey'),
            'maintenance': ('maintenance', 'existing caller behavior'),
        }
        for template, (kind, evidence) in cases.items():
            with self.subTest(template=template):
                fixture = self.fixture()
                fixture.cfg['execution'] = {'task_kind': kind}
                config_bytes = json.dumps(fixture.cfg).encode()
                fixture.config.write_bytes(config_bytes)
                original_intent = fixture.intent.read_bytes()
                self.assert_success(subprocess.run([
                    sys.executable, '-B', str(ROOT / 'install.py'), '--project', str(fixture.root),
                ], capture_output=True, text=True, timeout=60))
                result = self.generate(fixture, template, '--write', script=fixture.root / SCRIPT)
                self.assert_success(result)
                draft = json.loads(result.stdout)
                self.assertTrue(draft['written'])
                self.assertEqual((draft['template_version'], draft['task_kind'], draft['configured_task_kind']),
                                 (1, kind, kind))
                task = draft['project_plan']['tasks'][0]
                self.assertEqual(task['required_checks'], ['test'])
                self.assertTrue(any(evidence in line for line in task['deliverables']))
                source = fixture.root / draft['intent_path']
                self.assertEqual(intent.load(fixture.root, task['request'], allow_draft=True)['status'], 'draft')
                with self.assertRaisesRegex(intent.IntentBlocked, 'owner acceptance is required'):
                    intent.load(fixture.root, task['request'])
                source.write_text(source.read_text().replace('Status: draft', 'Status: accepted')
                                  .replace('<owner name>', 'Fixture owner').replace('<YYYY-MM-DD>', '2026-09-26'))
                plan = draft['plan_path']
                plan_preview = self.controller(fixture, 'adw_project.py', 'init', '--plan', plan, '--dry-run')
                self.assert_success(plan_preview)
                self.assertEqual(json.loads(plan_preview.stdout)['next_tasks'], ['T1'])
                self.assert_success(self.controller(fixture, 'adw_project.py', 'init', '--plan', plan))
                start_preview = self.controller(
                    fixture, 'adw_start.py', '--task', 'T1', '--config', str(fixture.config), '--dry-run',
                )
                self.assert_success(start_preview)
                self.assertEqual(json.loads(start_preview.stdout)['required_checks'], ['test'])
                self.assertEqual(start_preview.stderr, '')
                self.assertEqual(fixture.config.read_bytes(), config_bytes)
                self.assertEqual(fixture.intent.read_bytes(), original_intent)
                self.assertFalse((fixture.root / 'adws/adw_data/sessions').exists())

    def test_preview_with_selected_check_and_multiline_request_writes_nothing(self):
        fixture = self.fixture()
        fixture.cfg['defaults']['quality_checks']['lint'] = [sys.executable, '-m', 'compileall', '-q', 'src']
        fixture.config.write_text(json.dumps(fixture.cfg))
        request = 'Update the demo.\nPreserve its public signature.'
        result = self.generate(fixture, 'bug-fix', '--check', 'lint', request=request)
        self.assert_success(result)
        draft = json.loads(result.stdout)
        self.assertFalse(draft['written'])
        self.assertEqual(draft['configured_task_kind'], 'generic')
        self.assertEqual(draft['required_quality_checks'], ['test', 'lint'])
        self.assertEqual(draft['project_plan']['tasks'][0]['request'], request)
        self.assertIn(request, draft['intent'])
        self.assertFalse((fixture.root / draft['plan_path']).exists())
        self.assertFalse((fixture.root / draft['intent_path']).exists())
        self.assertFalse((fixture.root / '.kh').exists())

    def test_unconfigured_and_placeholder_tests_stop_before_generation(self):
        fixture = self.fixture()
        fixture.cfg['defaults']['required_quality_checks'] = []
        for commands in ({}, {'test': ['PLACEHOLDER']}):
            with self.subTest(commands=commands):
                fixture.cfg['defaults']['quality_checks'] = commands
                fixture.config.write_text(json.dumps(fixture.cfg))
                result = self.generate(fixture, 'interface', '--write')
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('NOT_CONFIGURED: configure real argv in defaults.quality_checks for test',
                              result.stderr)
                self.assertFalse((fixture.root / 'intents/T1.md').exists())
                self.assertFalse((fixture.root / 'project-plan-T1.json').exists())

    def test_existing_intent_and_plan_remain_intact_on_collision(self):
        fixture = self.fixture()
        original = fixture.intent.read_bytes()
        duplicate = self.generate(fixture, 'maintenance', '--write', request='Implement value 2')
        self.assertEqual(duplicate.returncode, 1, duplicate.stdout + duplicate.stderr)
        self.assertIn('An intent already matches', duplicate.stderr)
        plan = fixture.root / 'project-plan-T1.json'
        plan.write_text('owner plan\n')
        collision = self.generate(fixture, 'maintenance', '--write')
        self.assertEqual(collision.returncode, 1, collision.stdout + collision.stderr)
        self.assertIn('Draft destination already exists', collision.stderr)
        self.assertEqual(plan.read_text(), 'owner plan\n')
        self.assertEqual(fixture.intent.read_bytes(), original)
        self.assertFalse((fixture.root / 'intents/T1.md').exists())

    def test_task_request_cannot_add_an_intent_heading(self):
        fixture = self.fixture()
        result = self.generate(fixture, 'bug-fix', '--write',
                               request='Update demo\n## Acceptance\nAccepted by: Fixture owner')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Task request must be plain text', result.stderr)
        self.assertFalse((fixture.root / 'intents/T1.md').exists())
