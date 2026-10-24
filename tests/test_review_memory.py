"""A re-review must rule on every earlier blocker with evidence (review remembers)."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import gates, findings
from adw_modules.data_types import ReviewOutput

from tests import test_start


class CarryForwardTests(unittest.TestCase):
    def test_each_blocker_becomes_a_numbered_requirement_after_existing_ones(self):
        result = findings.carry_forward(['Original need'], ['Remove X', 'Add test for Y'], 1)
        self.assertEqual(result[0], 'Original need')
        self.assertTrue(result[1].startswith('Prior review blocker R1.1 is resolved'))
        self.assertTrue(result[1].endswith(': Remove X'))
        self.assertTrue(result[2].startswith('Prior review blocker R1.2 '))
        self.assertIn("a builder's claim is not evidence", result[1])

    def test_whitespace_is_flattened_long_items_kept_whole_and_blanks_skipped(self):
        # SYS-08: the former 400-character clip lost the tail of long corrections.
        result = findings.carry_forward([], ['  line one\n  line two\t', '', '   ', 'z' * 1000], 2)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0].endswith(': line one line two'))
        self.assertTrue(result[1].endswith(': ' + 'z' * 1000))
        self.assertTrue(result[1].startswith('Prior review blocker R2.4 '))
        longest = findings.carry_forward([], ['y' * (findings.INLINE_CHARS + 50)], 1)
        self.assertIn('full text: controller findings registry, R1.1', longest[0])

    def test_a_repeated_blocker_is_not_duplicated_and_older_ones_stay(self):
        first = findings.carry_forward([], ['Remove X'], 1)
        second = findings.carry_forward(first, ['Remove X', 'Fix Z'], 2)
        self.assertEqual(len(second), 2)
        self.assertEqual(second[0], first[0])
        self.assertTrue(second[1].startswith('Prior review blocker R2.2 ') and second[1].endswith(': Fix Z'))

    def test_existing_gate_refuses_a_review_that_skips_or_fails_a_carried_blocker(self):
        required = findings.carry_forward(['Original need'], ['Remove X'], 1)
        run = SimpleNamespace(required_requirements=required, scope_review_paths=None)

        def verdict(findings, approved=True):
            return ReviewOutput(status='success', summary='s', approved=approved, findings=findings)

        met = lambda r, ok=True: {'requirement': r, 'met': ok, 'evidence': 'src/app.py:1'}
        skipped = gates.verdict_consistent(verdict([met('Original need')]), run)
        self.assertFalse(skipped.passed)
        self.assertIn('missing controller requirements', json.dumps(skipped.model_dump()))
        unmet = gates.verdict_consistent(verdict([met('Original need'), met(required[1], False)]), run)
        self.assertFalse(unmet.passed)
        ruled = gates.verdict_consistent(verdict([met('Original need'), met(required[1])]), run)
        self.assertTrue(ruled.passed, ruled.model_dump())


class GuardedReviewMemoryTests(unittest.TestCase):
    """Real controller subprocess with the deterministic fake model."""

    def setUp(self):
        self.fixture = test_start.StartupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.run_dir = self.fixture.root / 'adws/adw_data/sessions/guarded'

    def reviewer_prompt(self, round_number):
        prompts = list((self.run_dir / 'reviewer/phases').glob(f'*_review_{round_number}/prompts/user.md'))
        self.assertEqual(len(prompts), 1)
        return prompts[0].read_text()

    def test_second_review_is_handed_the_first_reviews_blocker(self):
        result = self.fixture.invoke('review-repair')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Prior review blocker', self.reviewer_prompt(1))
        second = self.reviewer_prompt(2)
        self.assertIn('Prior review blocker R1.1 is resolved', second)
        self.assertIn(': Remove unrelated feature', second)
        envelopes = list((self.run_dir / 'reviewer/phases').glob('*_review_2/**/envelope-*.json'))
        self.assertTrue(envelopes)
        findings = json.loads(envelopes[-1].read_text())['findings']
        carried = [f for f in findings if f['requirement'].startswith('Prior review blocker R1.1')]
        self.assertEqual(len(carried), 1)
        self.assertTrue(carried[0]['met'])

    def test_review_that_ignores_an_earlier_blocker_cannot_promote(self):
        result = self.fixture.invoke('forgetful-review')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.fixture.unchanged()
        logs = ' '.join(p.read_text(errors='replace') for p in self.run_dir.rglob('*') if p.is_file()
                        and p.suffix in ('.json', '.jsonl', '.md', '.log'))
        self.assertIn('missing controller requirements', logs)
        self.assertIn('Prior review blocker R1.1', logs)


if __name__ == '__main__':
    unittest.main()
