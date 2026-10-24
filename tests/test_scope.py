"""Controller scope persistence and path boundary regression tests."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
from pathlib import Path
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import delta
from adw_modules.permissions import Entry, Snapshot
from adw_modules.scope import (ScopeContract, ScopeViolation, capture_contract, effective_contract, plan_boundary,
    add_plan_requirements, check_paths, load_contract)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = SimpleNamespace(session_dir=self.root / 'sessions' / 'run',
                                   required_requirements=['Owner request: fix login'])

    def tearDown(self):
        self.temp.cleanup()

    def contract(self, **kwargs):
        return ScopeContract(original_request='Owner request: fix login',
            deliverables=['Login succeeds'], **kwargs)

    def test_original_requirements_survive_plan_and_are_deduplicated(self):
        contract = self.contract(constraints=['Preserve existing sessions'],
                                 non_goals=['Redesign settings'])
        capture_contract(self.run, contract)
        criteria = add_plan_requirements(self.run, ['Login succeeds', 'Test expired tokens'])
        self.assertEqual(criteria, ['Owner request: fix login', 'Login succeeds',
            'Preserve existing sessions', 'Out of scope: Redesign settings', 'Test expired tokens'])
        self.assertEqual(json.loads((self.run.session_dir/'acceptance.json').read_text()), criteria)
        # Capturing the same frozen scope on resume preserves accumulated criteria.
        capture_contract(self.run, contract)
        self.assertEqual(self.run.required_requirements, criteria)

    def test_contract_cannot_widen_on_resume(self):
        contract = self.contract(allowed_paths=['src/login.py'])
        capture_contract(self.run, contract)
        resumed = SimpleNamespace(session_dir=self.run.session_dir, required_requirements=[])
        with self.assertRaises(ScopeViolation):
            capture_contract(resumed, self.contract(allowed_paths=['**']))
        self.assertEqual(load_contract(self.run.session_dir/'scope.json'), contract)
        with self.assertRaises(ScopeViolation):
            load_contract(self.run.session_dir/'scope.json', 'Different request')

    def test_explicit_read_only_differs_from_inherited_role(self):
        self.assertEqual(check_paths(self.contract(), ['src/login.py']), ['src/login.py'])
        with self.assertRaises(ScopeViolation):
            check_paths(self.contract(allowed_paths=[]), ['src/login.py'])

    def test_only_current_phase_evidence_exempt_from_read_only(self):
        prefix = 'adws/adw_data/sessions/run/context_handoff/phase/reviewer'
        contract = self.contract(allowed_paths=[])
        self.assertEqual(check_paths(contract, [prefix+'/review.md'], prefix), [prefix+'/review.md'])
        for forbidden in [prefix+'-other/review.md', prefix.replace('/phase/', '/old/')+'/review.md',
                          '.kh/scope.json', 'src/login.py']:
            with self.subTest(forbidden=forbidden), self.assertRaises(ScopeViolation):
                check_paths(contract, [forbidden], prefix)

    def test_glob_scope_does_not_widen_single_star(self):
        contract = self.contract(allowed_paths=['src/*.py'])
        check_paths(contract, ['src/login.py'])
        with self.assertRaises(ScopeViolation):
            check_paths(contract, ['src/admin/login.py'])
        check_paths(self.contract(allowed_paths=['src/**/*.py']), ['src/login.py', 'src/admin/login.py'])

    def test_unsafe_paths_rejected(self):
        for value in ['../outside', '/tmp/write', 'src/../config', 'C:\\outside', '~/outside',
                      'src//file', 'src/./file', '', 'src/\x00file']:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.contract(allowed_paths=[value])

    def test_unknown_fields_and_invalid_checks_are_rejected(self):
        for kwargs in [{'required_checks':['unsupported']}, {'required_checks':['']},
                       {'review_required':'false'}, {'unrecognized':'quietly ignored'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.contract(**kwargs)
        self.assertTrue(self.contract().review_required)
        self.assertEqual(self.contract(required_checks=['test','lint','typecheck','build']).required_checks,
                         ['test','lint','typecheck','build'])
        for kwargs in [{'original_request':' ', 'deliverables':['done']},
                       {'original_request':'request', 'deliverables':[]},
                       {'original_request':'request', 'deliverables':[' ']}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                ScopeContract(**kwargs)

    def test_original_request_whitespace_preserved(self):
        request = '  Owner request\nwith exact formatting.  '
        contract = ScopeContract(original_request=request, deliverables=['done'])
        capture_contract(self.run, contract)
        self.assertEqual(load_contract(self.run.session_dir/'scope.json', request).original_request, request)
        with self.assertRaises(ScopeViolation):
            load_contract(self.run.session_dir/'scope.json', request.strip())

    def test_scope_symlink_never_followed(self):
        target = self.root/'owner.json'
        target.write_text(json.dumps(self.contract().model_dump()))
        self.run.session_dir.mkdir(parents=True)
        (self.run.session_dir/'scope.json').symlink_to(target)
        before = target.read_bytes()
        with self.assertRaises(ScopeViolation):
            capture_contract(self.run, self.contract())
        self.assertEqual(target.read_bytes(), before)

    def test_parent_and_acceptance_symlinks_never_followed(self):
        outside = self.root/'outside'
        outside.mkdir()
        self.run.session_dir.parent.mkdir()
        self.run.session_dir.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ScopeViolation):
            capture_contract(self.run, self.contract())
        self.assertEqual(list(outside.iterdir()), [])
        self.run.session_dir.unlink()
        self.run.session_dir.mkdir()
        victim = outside/'victim.json'
        victim.write_text('owner')
        (self.run.session_dir/'acceptance.json').symlink_to(victim)
        with self.assertRaises(ScopeViolation):
            add_plan_requirements(self.run, ['new criterion'])
        self.assertEqual(victim.read_text(), 'owner')
        self.assertEqual(self.run.required_requirements, ['Owner request: fix login'])

    def test_failed_atomic_replacement_preserves_previous_requirements(self):
        capture_contract(self.run, self.contract())
        before = (self.run.session_dir/'acceptance.json').read_bytes()
        with patch('adw_modules.scope.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(ScopeViolation):
                add_plan_requirements(self.run, ['new criterion'])
        self.assertEqual((self.run.session_dir/'acceptance.json').read_bytes(), before)
        self.assertNotIn('new criterion', self.run.required_requirements)
        self.assertEqual(list(self.run.session_dir.glob('.*.tmp')), [])

    def test_invalid_plan_cannot_remove_requirements(self):
        capture_contract(self.run, self.contract())
        before = self.run.required_requirements[:]
        for criteria in [[''], [None], 'replace everything']:
            with self.subTest(criteria=criteria), self.assertRaises(ScopeViolation):
                add_plan_requirements(self.run, criteria)
        self.assertEqual(self.run.required_requirements, before)



class PlanBoundaryTests(unittest.TestCase):
    def owner(self, allowed):
        return ScopeContract(original_request='fix login', deliverables=['login works'], allowed_paths=allowed)

    def test_plan_supplies_boundary_when_owner_gave_none(self):
        self.assertEqual(plan_boundary(self.owner(None), ['src/a.py', 'src/a.py', 'tests/test_a.py']),
                         ['src/a.py', 'tests/test_a.py'])

    def test_plan_narrows_but_never_widens_owner_scope(self):
        self.assertEqual(plan_boundary(self.owner(['src/**', 'tests/']), ['src/auth/login.py', 'tests/test_login.py']),
                         ['src/auth/login.py', 'tests/test_login.py'])
        with self.assertRaises(ScopeViolation) as caught:
            plan_boundary(self.owner(['src/**']), ['src/auth/login.py', 'README.md'])
        self.assertIn('README.md', str(caught.exception))

    def test_plan_pattern_cannot_widen_owner_pattern(self):
        # Compare a plan pattern against its expanded file paths: src/*.py
        # "matched" src/?.py and widened the boundary to every module.
        for owner, plan in [(['src/?.py'], ['src/*.py']), (['src/*.py'], ['src/**']), (['s*/'], ['src/a.py']),
                            (['src/'], ['srcx/a.py']), (['src/a?.py'], ['src/a*.py'])]:
            with self.subTest(owner=owner, plan=plan), self.assertRaises(ScopeViolation):
                plan_boundary(self.owner(owner), plan)
        for owner, plan in [(['src/**'], ['src/*.py']), (['src/'], ['src/api/*.py']), (['src/*.py'], ['src/*.py']),
                            (['**'], ['anything/**']), (['src/?.py'], ['src/a.py']), (['src/'], ['src'])]:
            with self.subTest(owner=owner, plan=plan):
                self.assertEqual(plan_boundary(self.owner(owner), plan), plan)

    def test_runtime_enforces_owner_and_plan_boundaries_together(self):
        both = ScopeContract(original_request='x', deliverables=['y'], allowed_paths=['src/?.py'], plan_paths=['src/*.py'])
        with self.assertRaises(ScopeViolation):
            check_paths(both, ['src/app.py'])          # matches the plan, not the owner
        with self.assertRaises(ScopeViolation):
            check_paths(ScopeContract(original_request='x', deliverables=['y'], allowed_paths=['src/'],
                                      plan_paths=['src/a.py']), ['src/b.py'])   # matches the owner, not the plan
        self.assertEqual(check_paths(both, ['src/a.py']), ['src/a.py'])

    def test_empty_or_unsafe_plan_paths_rejected(self):
        for proposed in ([], None, ['/etc/passwd'], ['../escape.py'], [''], [42]):
            with self.subTest(proposed=proposed), self.assertRaises((ScopeViolation, ValueError)):
                plan_boundary(self.owner(None), proposed)

    def test_effective_contract_is_frozen_per_run_and_keeps_owner_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = SimpleNamespace(session_dir=Path(temporary))
            owner = self.owner(None)
            effective = effective_contract(run, owner, ['src/a.py'])
            self.assertEqual(effective.allowed_paths, ['src/a.py'])
            self.assertEqual(effective.plan_paths, ['src/a.py'])
            self.assertIsNone(owner.allowed_paths)
            scoped = effective_contract(SimpleNamespace(session_dir=Path(temporary) / 'b'), self.owner(['src/**']), ['src/*.py'])
            self.assertEqual((scoped.allowed_paths, scoped.plan_paths), (['src/**'], ['src/*.py']))
            self.assertEqual(json.loads((Path(temporary) / 'effective_scope.json').read_text())['allowed_paths'], ['src/a.py'])
            effective_contract(run, owner, ['src/a.py'])          # identical rerun is fine
            with self.assertRaises(ScopeViolation):
                effective_contract(run, owner, ['src/b.py'])      # a resumed run cannot move the boundary


class DeltaTests(unittest.TestCase):
    def snap(self, files):
        return Snapshot({k: Entry('file', 0o644, v) for k, v in files.items()}, Path('.'), {})

    def test_unified_diff_reports_controller_observed_bytes(self):
        before = self.snap({'src/a.py': b'value = 1\n', 'gone.py': b'x\n'})
        after = self.snap({'src/a.py': b'value = 2\n', 'new.py': b'y\n'})
        text = delta.unified_diff(before, after, ['src/a.py', 'gone.py', 'new.py'])
        self.assertIn('-value = 1', text); self.assertIn('+value = 2', text)
        self.assertIn('+++ /dev/null', text); self.assertIn('--- /dev/null', text)
        self.assertEqual(delta.unified_diff(before, before, []), '(no source changes)\n')

    def test_binary_changes_are_flagged_with_size_and_hash(self):
        before = self.snap({'img.bin': b'\xff\xfe\x00'}); after = self.snap({'img.bin': b'\x00\x01'})
        text = delta.unified_diff(before, after, ['img.bin'])
        self.assertIn('binary change', text); self.assertIn('3 bytes sha256', text); self.assertIn('2 bytes sha256', text)

    def test_missing_trailing_newline_does_not_merge_lines(self):
        text = delta.unified_diff(self.snap({'f': b'a'}), self.snap({'f': b'b'}), ['f'])
        self.assertNotIn('-a+b', text)
        self.assertIn('-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file', text)

    def test_mode_only_symlink_and_kind_changes_are_explicit(self):
        text = delta.unified_diff(self.snap({'f': b'a\n'}),
                                  Snapshot({'f': Entry('file', 0o755, b'a\n')}, Path('.'), {}), ['f'])
        self.assertIn('old mode 0644\nnew mode 0755', text)
        links = delta.unified_diff(Snapshot({'l': Entry('symlink', 0o777, b'x')}, Path('.'), {}),
                                   Snapshot({'l': Entry('symlink', 0o777, b'y')}, Path('.'), {}), ['l'])
        self.assertIn('before: symlink -> x', links); self.assertIn('after:  symlink -> y', links)
        kind = delta.unified_diff(self.snap({'p': b'a\n'}), Snapshot({'p': Entry('symlink', 0o777, b'a')}, Path('.'), {}), ['p'])
        self.assertIn('before: file', kind); self.assertIn('after:  symlink', kind)

    def test_verification_paths_heuristic_and_configured_checks(self):
        cfg = SimpleNamespace(defaults=SimpleNamespace(quality_checks={'test': ['python', 'run_checks.py']}))
        paths = ['src/app.py', 'tests/test_app.py', 'app.spec.ts', 'pytest.ini', 'run_checks.py', 'docs/guide.md']
        self.assertEqual(delta.verification_paths(paths, cfg), ['tests/test_app.py', 'app.spec.ts', 'pytest.ini', 'run_checks.py'])
        self.assertEqual(delta.verification_paths(['src/app.py'], None), [])

if __name__ == '__main__':
    unittest.main()
