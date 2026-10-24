"""Sanctioned plan changes through literal CLI argv, including paths with spaces."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from tests import test_project_revisions as revisions
from tests import test_project as fixture
from adw_modules import project


class ProjectRevisionCliTests(unittest.TestCase):
    setUp = fixture.ProjectTests.setUp
    tearDown = fixture.ProjectTests.tearDown
    files = fixture.ProjectTests.files
    init = revisions.ProjectRevisionTests.init
    definition = revisions.ProjectRevisionTests.definition
    children = revisions.ProjectRevisionTests.children
    accepted = revisions.ProjectRevisionTests.accepted

    def invoke(self, operation, payload, *extra):
        path = Path(self.temp.name) / 'قرار الخطة $ literal.json'
        path.write_text(json.dumps(payload, ensure_ascii=False))
        flag = '--definition' if operation == 'amend' else '--plan'
        return subprocess.run([sys.executable, '-B', str(fixture.ADWS / 'adw_project.py'),
            '--root', str(self.root), operation, '--task', 'T1', flag, str(path),
            '--reason', 'تقسيم آمن $(touch INJECTED) `literal`', *extra],
            capture_output=True, text=True, timeout=15)

    def test_amend_cli_previews_then_applies_exact_owner_request(self):
        self.init()
        definition = self.definition(request='تنفيذ وحدة أصغر $ حرفي')
        self.accepted(definition)
        before = self.files()
        preview = self.invoke('amend', definition, '--dry-run')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(json.loads(preview.stdout)['dry_run'])
        self.assertEqual(self.files(), before)
        applied = self.invoke('amend', definition)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(project.load_task(self.root, 'T1')['request'], 'تنفيذ وحدة أصغر $ حرفي')
        self.assertEqual(json.loads(applied.stdout)['revision']['operation'], 'amend')
        self.assertFalse((self.root / 'INJECTED').exists())

    def test_split_cli_rewires_downstream_after_dry_run(self):
        self.init()
        children = self.children()
        for definition in children:
            self.accepted(definition)
        before = self.files()
        preview = self.invoke('split', children, '--dry-run')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(self.files(), before)
        applied = self.invoke('split', children)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(project.load_task(self.root, 'T2')['dependencies'], ['T1b'])
        self.assertEqual(json.loads(applied.stdout)['revision']['task_ids'], ['T1', 'T1b'])

    def test_cli_requires_acceptance_and_payload_shape_without_mutating_ledger(self):
        self.init()
        before = self.files()
        result = self.invoke('amend', self.definition(request='Not accepted'))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('accepted', result.stderr)
        self.assertEqual(self.files(), before)
        malformed = self.invoke('split', {'tasks': self.children()})
        self.assertEqual(malformed.returncode, 1, malformed.stderr)
        self.assertIn('list', malformed.stderr)
        self.assertEqual(self.files(), before)
