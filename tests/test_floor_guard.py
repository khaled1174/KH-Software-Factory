"""Floor guard: moves that lower the quality bar become reviewer requirements."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from pydantic import ValidationError
from adw_modules import floor_guard
from adw_modules.data_types import KHConfig
from adw_modules.permissions import Entry

from tests import test_start


class _Snapshot:
    def __init__(self, files):
        self.entries = {p: Entry('file', 0o644, data) for p, data in files.items()}


def scan(before, after):
    return floor_guard.scan(_Snapshot(before), _Snapshot(after), sorted(set(before) | set(after)))


def rules(before, after):
    return [(f['rule'], f['path']) for f in scan(before, after)]


class ScanTests(unittest.TestCase):
    def test_ordinary_change_is_clean(self):
        self.assertEqual(scan({'src/a.py': b'x = 1\n'}, {'src/a.py': b'x = 2\n'}), [])

    def test_silenced_checkers_in_any_file(self):
        for line in (b'x = f()  # noqa: E501', b'y = g()  # type: ignore[arg-type]', b'// eslint-disable-next-line',
                     b'// @ts-ignore', b'/* istanbul ignore next */', b'k = "x"  # nosec', b'# pragma: no cover'):
            with self.subTest(line=line):
                self.assertEqual(rules({'src/a.py': b'x = 1\n'}, {'src/a.py': b'x = 1\n' + line + b'\n'}),
                                 [('silenced-checker', 'src/a.py')])

    def test_tests_made_easier_only_in_test_files(self):
        for line in (b'@pytest.mark.skip', b'@pytest.mark.xfail(reason="later")', b'@unittest.skip("x")',
                     b'it.only("runs alone", () => {})', b'describe.skip("x", () => {})', b'xit("x", () => {})',
                     b'        self.skipTest("later")'):
            with self.subTest(line=line):
                self.assertEqual(rules({'tests/test_a.py': b'def test_a():\n    assert 1\n'},
                                       {'tests/test_a.py': line + b'\ndef test_a():\n    assert 1\n'}),
                                 [('test-made-easier', 'tests/test_a.py')])
        self.assertEqual(rules({}, {'src/list.js': b'items.skip(2)\n'}), [])

    def test_deleted_test_file_and_fewer_assertions(self):
        self.assertEqual(rules({'tests/test_a.py': b'def test_a():\n    assert 1\n'}, {}),
                         [('test-deleted', 'tests/test_a.py')])
        two = b'def test_a():\n    assert f() == 1\n    assert f() > 0\n'
        self.assertEqual(rules({'tests/test_a.py': two}, {'tests/test_a.py': b'def test_a():\n    assert f() == 1\n'}),
                         [('assertions-removed', 'tests/test_a.py')])
        moved = b'def test_a():\n    assert f() > 0\n    assert f() == 1\n'
        self.assertEqual(rules({'tests/test_a.py': two}, {'tests/test_a.py': moved}), [])   # reordered, not fewer
        self.assertEqual(rules({'src/a.py': b'assert x\n'}, {}), [])                         # not a test file

    def test_unfinished_work_in_source(self):
        cases = {
            b'    raise NotImplementedError': 'raise NotImplementedError',
            b'    throw new Error("Not implemented yet")': 'not implemented',
            b'    except ValueError: pass': 'except',
            b'  } catch (e) {}': 'catch',
            b'# TODO: finish the refund path': 'TODO',
        }
        for line, marker in cases.items():
            with self.subTest(line=line):
                found = scan({'src/a.py': b'x = 1\n'}, {'src/a.py': b'x = 1\n' + line + b'\n'})
                self.assertEqual([f['rule'] for f in found], ['unfinished-work'])
                self.assertIn(marker.lower(), found[0]['marker'].lower())
        two_line = b'try:\n    run()\nexcept Exception:\n    pass\n'
        self.assertEqual(rules({'src/a.py': b'run()\n'}, {'src/a.py': two_line}), [('unfinished-work', 'src/a.py')])
        # The word "todo" in a to-do app is not the TODO marker.
        self.assertEqual(rules({}, {'src/todo.py': b'add_todo("a todo item")\ntodos = []\n'}), [])

    def test_thresholds_loosened_or_removed_in_check_configuration(self):
        before = {'pyproject.toml': b'[tool.coverage.report]\nfail_under = 90\n',
                  'package.json': b'{"scripts": {"lint": "eslint . --max-warnings 0"}}\n'}
        looser = {'pyproject.toml': b'[tool.coverage.report]\nfail_under = 70\n',
                  'package.json': b'{"scripts": {"lint": "eslint . --max-warnings 25"}}\n'}
        found = scan(before, looser)
        self.assertEqual(sorted((f['rule'], f['path'], f['marker']) for f in found),
                         [('threshold-loosened', 'package.json', 'max-warnings 0 -> 25'),
                          ('threshold-loosened', 'pyproject.toml', 'fail_under 90 -> 70')])
        tighter = {'pyproject.toml': b'[tool.coverage.report]\nfail_under = 95\n',
                   'package.json': b'{"scripts": {"lint": "eslint . --max-warnings 0"}}\n'}
        self.assertEqual(scan(before, tighter), [])                 # tightening is silent
        self.assertEqual(rules(before, {**before, 'pyproject.toml': b'[tool.coverage.report]\n'}),
                         [('threshold-removed', 'pyproject.toml')])
        self.assertEqual(rules({'src/a.py': b'lines = 90\n'}, {'src/a.py': b'lines = 10\n'}), [])  # not config

    def test_findings_never_echo_line_text(self):
        secret = b'API_KEY = "sk_live_51NotARealKeyButLooksLikeOne"  # gitleaks:allow'
        found = scan({'src/a.py': b'x = 1\n'}, {'src/a.py': b'x = 1\n' + secret + b'\n'})
        self.assertEqual(found, [{'rule': 'silenced-checker', 'path': 'src/a.py', 'line': 2,
                                  'marker': 'gitleaks:allow'}])
        self.assertNotIn('sk_live', json.dumps(found) + floor_guard.requirement(found[0]))

    def test_non_utf8_text_and_binaries(self):
        self.assertEqual(rules({}, {'src/a.py': 'café = 1  # noqa\n'.encode('latin-1')}),
                         [('silenced-checker', 'src/a.py')])
        self.assertEqual(rules({}, {'img.png': b'\x89PNG\x00# noqa'}), [])

    def test_requirements_track_the_current_candidate(self):
        first = scan({'src/a.py': b'x\n'}, {'src/a.py': b'x  # noqa\n'})
        reqs = floor_guard.replace_requirements(['Original need'], first)
        self.assertEqual(len(reqs), 2)
        self.assertTrue(reqs[1].startswith('Floor guard: silenced-checker at src/a.py:1 (# noqa)'))
        self.assertEqual(floor_guard.replace_requirements(reqs, []), ['Original need'])   # fixed in a repair

    def test_configuration(self):
        self.assertTrue(KHConfig().execution.floor_guard.enabled)
        self.assertFalse(KHConfig.model_validate({'execution': {'floor_guard': {'enabled': False}}})
                         .execution.floor_guard.enabled)
        with self.assertRaises(ValidationError):
            KHConfig.model_validate({'execution': {'floor_guard': {'enabled': 'no'}}})


class GuardedStartFloorTests(unittest.TestCase):
    """Real controller subprocess with the deterministic fake model."""

    def setUp(self):
        self.fixture = test_start.StartupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.run_dir = self.fixture.root / 'adws/adw_data/sessions/guarded'

    def test_reviewer_must_rule_on_a_silenced_checker(self):
        result = self.fixture.invoke('floor-noqa')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompt = next(self.run_dir.glob('reviewer/phases/*_review_1/prompts/user.md')).read_text()
        self.assertIn('Floor guard: silenced-checker at src/app.py:1 (# type: ignore)', prompt)
        evidence = json.loads((self.run_dir / 'context_handoff/floor_guard_1.json').read_text())
        self.assertEqual(evidence, [{'rule': 'silenced-checker', 'path': 'src/app.py', 'line': 1,
                                     'marker': '# type: ignore'}])

    def test_review_that_ignores_the_floor_cannot_promote(self):
        result = self.fixture.invoke('floor-forgetful')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.fixture.unchanged()



if __name__ == '__main__':
    unittest.main()
