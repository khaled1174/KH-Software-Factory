"""Sensitive changes stop before promotion unless the accepted intent approves them."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from pydantic import ValidationError
from adw_modules import owner_review
from adw_modules.data_types import OwnerReviewSettings, KHConfig
from adw_modules.permissions import Entry

from tests import test_start


class _Snapshot:
    def __init__(self, files):
        self.entries = {p: Entry('file', 0o644, data) for p, data in files.items()}


def changes(before, after):
    paths = sorted(set(before) | set(after))
    return owner_review.changed(_Snapshot(before), _Snapshot(after), paths)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.settings = OwnerReviewSettings()

    def categories(self, delta, approved=()):
        return [h['category'] for h in owner_review.evaluate(self.settings, delta, approved)]

    def test_defaults_ship_enabled_with_money_auth_schema_and_size(self):
        self.assertTrue(KHConfig().execution.owner_review.enabled)
        self.assertEqual(owner_review.known_categories(self.settings), {'money', 'auth', 'schema', 'size'})
        self.assertEqual(self.settings.max_changed_lines, 400)

    def test_ordinary_change_promotes(self):
        self.assertEqual(self.categories(changes({'src/app.py': b'value = 1\n'},
                                                 {'src/app.py': b'value = 2\n'})), [])

    def test_paths_match_at_any_depth_including_root(self):
        for path, category in (('src/billing/charge.py', 'money'), ('billing/charge.py', 'money'),
                               ('auth/session.ts', 'auth'), ('app/api/authz/policy.py', 'auth')):
            with self.subTest(path=path):
                self.assertEqual(self.categories(changes({}, {path: b'x = 1\n'})), [category])
        # `*` stops at a separator, so a similarly named file is not a directory match.
        self.assertEqual(self.categories(changes({}, {'src/author.py': b'x = 1\n'})), [])

    def test_keywords_match_added_lines_only_case_insensitively(self):
        added = changes({'db/m.sql': b'select 1;\n'}, {'db/m.sql': b'select 1;\nDROP TABLE users;\n'})
        self.assertEqual(self.categories(added), ['schema'])
        removed = changes({'db/m.sql': b'select 1;\ndrop table users;\n'}, {'db/m.sql': b'select 1;\n'})
        self.assertEqual(self.categories(removed), [])
        hold = owner_review.evaluate(self.settings, added)[0]
        self.assertIn('db/m.sql', hold['why'])
        self.assertIn('drop table', hold['why'])

    def test_deleted_file_still_matches_its_path(self):
        self.assertEqual(self.categories(changes({'src/payments/refund.py': b'a\n'}, {})), ['money'])

    def test_size_counts_added_and_removed_lines_outside_exclusions(self):
        before = {'src/a.py': b''.join(b'old %d\n' % i for i in range(200))}
        after = {'src/a.py': b''.join(b'new %d\n' % i for i in range(200))}
        self.assertEqual(self.categories(changes(before, after)), [])          # exactly 400
        after['src/b.py'] = b'one more\n'
        self.assertEqual(self.categories(changes(before, after)), ['size'])    # 401
        lock = {'package-lock.json': b''.join(b'"x%d": 1,\n' % i for i in range(5000))}
        self.assertEqual(self.categories(changes({}, lock)), [])
        self.settings.max_changed_lines = 0
        self.assertEqual(self.categories(changes(before, after)), [])

    def test_owner_approvals_skip_only_the_named_categories(self):
        delta = changes({}, {'src/billing/x.py': b'jwt.sign(payload)\n'})
        self.assertEqual(self.categories(delta), ['auth', 'money'])
        self.assertEqual(self.categories(delta, ['money']), ['auth'])
        self.assertEqual(self.categories(delta, ['money', 'auth']), [])

    def test_disabled_policy_holds_nothing(self):
        self.settings.enabled = False
        self.assertEqual(self.categories(changes({}, {'src/auth/x.py': b'DROP TABLE t;\n'})), [])

    def test_unknown_approvals_are_reported(self):
        self.assertEqual(owner_review.unknown_approvals(self.settings, ['auth', 'payment', 'size']), ['payment'])

    def test_configuration_is_validated_and_keywords_lowercased(self):
        custom = OwnerReviewSettings(categories={'infra': {'paths': ['deploy/**'], 'keywords': ['TERRAFORM DESTROY']}})
        self.assertEqual(owner_review.known_categories(custom), {'infra', 'size'})
        self.assertEqual(custom.categories['infra'].keywords, ['terraform destroy'])
        self.assertEqual([h['category'] for h in owner_review.evaluate(
            custom, changes({}, {'ops/run.sh': b'terraform destroy -auto-approve\n'}))], ['infra'])
        for bad in ({'categories': {'size': {}}}, {'categories': {'Auth': {}}},
                    {'categories': {'x': {'paths': ['/etc/**']}}}, {'categories': {'x': {'paths': ['../x']}}},
                    {'categories': {'x': {'keywords': [' ']}}}, {'enabled': 'yes'}, {'max_changed_lines': -1},
                    {'unknown': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                OwnerReviewSettings.model_validate(bad)

    def test_non_utf8_text_cannot_hide_keywords_or_size(self):
        cp1252 = changes({'db/m.sql': b'select 1;\n'},
                         {'db/m.sql': 'select 1;\n-- café\nDROP TABLE users;\n'.encode('cp1252')})
        self.assertEqual(self.categories(cp1252), ['schema'])
        many = changes({}, {'src/legacy.txt': ''.join(f'línea {i}\n' for i in range(401)).encode('latin-1')})
        self.assertEqual(self.categories(many), ['size'])
        # NUL-bearing binaries (images, archives) are path-matched only.
        binary = changes({}, {'assets/logo.png': b'\x89PNG\x00\n' * 1000 + b'drop table'})
        self.assertEqual(self.categories(binary), [])
        self.assertEqual(self.categories(changes({}, {'src/auth/key.bin': b'\x00\x01'})), ['auth'])

    def test_paths_match_case_insensitively_and_wildcard_dir_patterns_cover_children(self):
        for path in ('app/Http/Controllers/Auth/LoginController.php', 'src/Payments/Charge.cs', 'BILLING/x.py'):
            with self.subTest(path=path):
                self.assertTrue(self.categories(changes({}, {path: b'x\n'})))
        custom = OwnerReviewSettings(categories={'infra': {'paths': ['**/Deploy/', 'ops/']}})
        for path in ('deploy/run.sh', 'a/b/deploy/c.yml', 'ops/x', 'OPS/y'):
            with self.subTest(path=path):
                self.assertEqual([h['category'] for h in owner_review.evaluate(custom, changes({}, {path: b'x\n'}))],
                                 ['infra'])

    def test_patterns_that_can_never_match_are_rejected(self):
        for bad in ('./src/billing/**', 'src\\billing\\**', 'src//billing/**', 'src/./billing/**', '.'):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                OwnerReviewSettings.model_validate({'categories': {'money': {'paths': [bad]}}})
        with self.assertRaises(ValidationError):
            OwnerReviewSettings.model_validate({'size_exclude': ['./package-lock.json']})
        OwnerReviewSettings.model_validate({'categories': {'money': {'paths': ['src/billing/', '**/.stripe/**']}}})

    def test_hold_message_names_reasons_evidence_and_the_owner_step(self):
        message = owner_review.hold_message([{'category': 'auth', 'why': 'path a matches b'}], 'x/diff.patch')
        self.assertTrue(message.startswith('OWNER_REVIEW_REQUIRED: auth: path a matches b'))
        self.assertIn('Nothing was promoted', message)
        self.assertIn('x/diff.patch', message)
        self.assertIn('Approved sensitive changes', message)


class GuardedStartOwnerReviewTests(unittest.TestCase):
    """Real controller subprocess with the deterministic fake model."""

    def setUp(self):
        self.fixture = test_start.StartupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.run_dir = self.root / 'adws/adw_data/sessions/guarded'
        # Treat the fixture's only source file as an owner-review category for these tests.
        self.configure()

    def configure(self, **settings):
        self.fixture.cfg['execution'] = {'owner_review': {'categories': {'app': {'paths': ['src/app.py']}}, **settings}}
        self.fixture.config.write_text(json.dumps(self.fixture.cfg))

    def approve(self, value):
        text = self.fixture.intent.read_text()
        body = ''.join(line for line in text.splitlines(keepends=True)
                       if not line.startswith('Accepted digest:'))
        self.fixture.intent.write_text(test_start.stamp_acceptance(body + f'Approved sensitive changes: {value}\n'))

    # Default: report mode. The run never stops for the owner.

    def test_default_report_mode_promotes_and_the_reviewer_rules_on_the_flag(self):
        result = self.fixture.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')
        prompt = next(self.run_dir.glob('reviewer/phases/*_review_1/prompts/user.md')).read_text()
        self.assertIn('Owner attention (app): path src/app.py matches src/app.py', prompt)
        self.assertEqual(json.loads((self.run_dir / 'context_handoff/owner_review_1.json').read_text()),
                         [{'category': 'app', 'why': 'path src/app.py matches src/app.py'}])
        self.assertIn('For the owner after completion: app: path src/app.py', result.stdout + result.stderr)

    def test_reviewer_that_ignores_an_owner_attention_item_cannot_promote(self):
        result = self.fixture.invoke('owner-forgetful')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.fixture.unchanged()

    def test_tracked_task_closes_itself_and_reports_the_flag_to_the_owner(self):
        from adw_modules import project
        from tests import test_corrective_release as corrective
        corrective.CorrectiveReleaseTests.init_plan(self.fixture)
        result = corrective.CorrectiveReleaseTests.tracked(self.fixture)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        task = project.load_task(self.root, 'T1')
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['receipt']['owner_review']['mode'], 'report')
        self.assertEqual(task['receipt']['owner_review']['flagged'],
                         [{'category': 'app', 'why': 'path src/app.py matches src/app.py'}])
        report = (self.root / '.kh/task-reports/T1.md').read_text()
        self.assertIn('## For the owner (after completion)', report)
        self.assertIn('Sensitive change — app: path src/app.py matches src/app.py', report)

    def test_batch_runs_unattended_from_owner_acceptance_to_completion(self):
        """The owner accepts intents and starts a batch; nothing asks them anything until it ends."""
        import subprocess, sys
        from adw_modules import intent, project
        accepted = ''.join(line for line in self.fixture.intent.read_text().splitlines(keepends=True)
                           if not line.startswith('Accepted digest:'))
        self.fixture.intent.write_text(accepted)  # accepted by name and date only: no digest to paste
        (self.root / 'intents/extend-value.md').write_text(
            accepted.replace('Implement value 2', 'Extend value to 22').replace('returns value 2.', 'returns value 22.'))
        task = {'id': 'T1', 'title': 'Change value', 'milestone_id': 'M1', 'request': 'Implement value 2',
                'deliverables': ['Update value'], 'non_goals': ['Unrelated features'],
                'allowed_paths': ['src/app.py'], 'weight': 1}
        project.init_project(self.root, {'schema_version': 1, 'project': {'id': 'P1', 'name': 'Fixture'},
            'milestones': [{'id': 'M1', 'title': 'Implementation'}],
            'tasks': [task, {**task, 'id': 'T2', 'title': 'Extend value', 'request': 'Extend value to 22',
                             'dependencies': ['T1']}]})
        result = subprocess.run([sys.executable, str(test_start.ADWS / 'adw_project.py'), 'run-continuous'], cwd=self.root,
                                env={**self.fixture.env, 'KH_FIXTURE_MODE': 'batch'},
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 22\n')
        ledger = json.loads((self.root / intent.LEDGER).read_text())['intents']
        self.assertEqual(sorted(ledger), ['intents/change-value.md', 'intents/extend-value.md'])
        for task_id in ('T1', 'T2'):
            with self.subTest(task=task_id):
                record = project.load_task(self.root, task_id)
                self.assertEqual(record['status'], 'completed')
                owner = record['receipt']['owner_review']
                self.assertEqual(owner['intent_binding'], 'first-use')
                self.assertEqual(owner['intent_accepted_digest'], ledger[owner['intent_path']]['digest'])
                self.assertEqual([f['category'] for f in owner['flagged']], ['app'])
                report = (self.root / f'.kh/task-reports/{task_id}.md').read_text()
                self.assertIn('Sensitive change — app: path src/app.py matches src/app.py', report)
        self.assertEqual((result.stdout + result.stderr).count('For the owner after completion: app'), 2)
        # The bound text now guards the intent: an edit after acceptance stops its next use.
        (self.root / 'intents/extend-value.md').write_text(
            (self.root / 'intents/extend-value.md').read_text().replace('returns value 22.', 'returns 23.'))
        with self.assertRaisesRegex(intent.IntentBlocked, 'changed after acceptance'):
            intent.load(self.root, 'Extend value to 22')

    # Opt-in hold mode: a human gate before promotion.

    def test_hold_mode_stops_and_keeps_the_diff(self):
        self.configure(mode='hold')
        result = self.fixture.invoke()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('OWNER_REVIEW_REQUIRED: app: path src/app.py matches src/app.py', result.stderr)
        self.fixture.unchanged()
        patches = list((self.run_dir / 'context_handoff').glob('controller_diff_*.patch'))
        self.assertEqual(len(patches), 1)
        self.assertIn('+value = 2', patches[0].read_text())

    def test_hold_mode_promotes_an_owner_approved_category(self):
        self.configure(mode='hold')
        self.approve('app')
        result = self.fixture.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')

    def test_unknown_approval_fails_before_any_model_dispatch(self):
        self.approve('ap')
        result = self.fixture.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown sensitive-change categories: ap', result.stderr)
        self.assertFalse((self.root / 'adws/adw_data').exists())
        self.fixture.unchanged()

    def test_disabled_policy_flags_nothing(self):
        self.configure(enabled=False)
        result = self.fixture.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompt = next(self.run_dir.glob('reviewer/phases/*_review_1/prompts/user.md')).read_text()
        self.assertNotIn('Owner attention', prompt)

    def test_mode_is_validated(self):
        from adw_modules.data_types import OwnerReviewSettings
        self.assertEqual(OwnerReviewSettings().mode, 'report')
        with self.assertRaises(ValidationError):
            OwnerReviewSettings.model_validate({'mode': 'ask'})


if __name__ == '__main__':
    unittest.main()
