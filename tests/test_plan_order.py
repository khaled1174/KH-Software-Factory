"""Steps that use something a later step creates are flagged to the plan reviewer."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import sys
import tempfile
import unittest

from tests import test_start as startup
sys.path.insert(0, str(startup.ADWS))
from adw_modules import planning


def plan(steps):
    ids = sorted({r for s in steps for r in s.get('requirement_ids', ['R1'])})
    return planning.ReviewedPlanOutput(
        status='success', summary='plan', artifacts=['plan.md'], acceptance_criteria=['works'],
        allowed_paths=sorted({p for s in steps for p in s['paths']}),
        steps=[{'requirement_ids': ['R1'], 'validation': ['check'], **s} for s in steps],
        requirement_coverage={i: [s['id'] for s in steps] for i in ids}, required_checks=['test'])


class OrderWarningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root, ignore_errors=True))
        (self.root/'app/Sources').mkdir(parents=True)
        (self.root/'app/Sources/Extractor.swift').write_text('struct Extractor {}\n')

    def test_type_used_before_the_step_that_creates_its_file(self):
        found = planning.order_warnings(plan([
            {'id': 'S6', 'action': 'Add extraction tests that inject FakeExtractor', 'paths': ['app/Tests/ExtractionTests.swift']},
            {'id': 'S7', 'action': 'Create the test double', 'paths': ['app/Tests/Fakes/FakeExtractor.swift']}]), self.root)
        self.assertEqual(found, [{'step_id': 'S6', 'uses': 'FakeExtractor',
                                  'path': 'app/Tests/Fakes/FakeExtractor.swift', 'created_by': 'S7'}])

    def test_correct_order_existing_files_and_generic_names_are_quiet(self):
        self.assertEqual(planning.order_warnings(plan([
            {'id': 'S1', 'action': 'Create FakeExtractor', 'paths': ['app/Tests/Fakes/FakeExtractor.swift']},
            {'id': 'S2', 'action': 'Use FakeExtractor and Extractor in tests', 'paths': ['app/Tests/ExtractionTests.swift']}]),
            self.root), [])
        self.assertEqual(planning.order_warnings(plan([
            {'id': 'S1', 'action': 'Refactor Extractor and update the index', 'paths': ['app/Sources/Extractor.swift']},
            {'id': 'S2', 'action': 'Add an index', 'paths': ['app/Sources/index.ts']}]), self.root), [])
        # A word that merely contains the stem is not a use of it.
        self.assertEqual(planning.order_warnings(plan([
            {'id': 'S1', 'action': 'Wire FakeExtractorFactory', 'paths': ['app/Sources/Extractor.swift']},
            {'id': 'S2', 'action': 'Create', 'paths': ['app/Tests/FakeExtractor.swift']}]), self.root), [])

    def test_full_path_mention_is_flagged_even_for_generic_names(self):
        found = planning.order_warnings(plan([
            {'id': 'S1', 'action': 'Import from src/utils.ts', 'paths': ['src/a.ts']},
            {'id': 'S2', 'action': 'Create helpers', 'paths': ['src/utils.ts']}]), self.root)
        self.assertEqual([(w['step_id'], w['uses']) for w in found], [('S1', 'src/utils.ts')])


class OrderWarningRunTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    invoke = startup.StartupTests.invoke

    def test_plan_reviewer_receives_the_order_checks(self):
        self.scope.write_text(json.dumps({'original_request': 'Implement value 2', 'deliverables': ['Update value'],
            'non_goals': ['Unrelated features'], 'allowed_paths': ['src/app.py', 'src/FakeValue.py']}))
        result = self.invoke('plan-order')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        session = self.root/'adws/adw_data/sessions/guarded'
        recorded = json.loads((session/'planning/order-1.json').read_text())
        self.assertEqual(recorded, [{'step_id': 'S1', 'uses': 'FakeValue', 'path': 'src/FakeValue.py', 'created_by': 'S2'}])
        prompts = [p.read_text() for p in (session/'plan_reviewer/phases').rglob('attempt-001/user.md')
                   if 'plan_digest' in p.read_text()]
        self.assertTrue(prompts and all('"controller_order_checks": [{"step_id": "S1"' in p for p in prompts), prompts[:1])


if __name__ == '__main__':
    unittest.main()
