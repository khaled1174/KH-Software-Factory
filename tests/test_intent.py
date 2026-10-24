"""Accepted intent must bind the exact task before any planning dispatch."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
from adw_modules import intent


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kh-intent-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'intents').mkdir()
        self.path = self.root / 'intents' / 'task.md'
        self.source = '''# Intent: Example

Status: accepted
Author: Originator
Date: 2026-09-26

## Task request

Build the agreed feature

## Problem

The feature is missing for users.

## Proposed outcome

The feature works for users.

## Acceptance

Accepted by: Project owner
Accepted on: 2026-09-26
'''
        self.unstamped = self.source
        self.source = self.stamp(self.source)
        self.path.write_text(self.source)

    @staticmethod
    def stamp(text):
        # Independent of intent.content_digest: the documented contract is the
        # SHA-256 of the file without its `Accepted digest` line.
        return text + 'Accepted digest: ' + hashlib.sha256(text.encode()).hexdigest() + '\n'

    def test_accepted_intent_binds_exact_request_and_review_context(self):
        record = intent.load(self.root, 'Build the agreed feature')
        self.assertEqual(record['sha256'], hashlib.sha256(self.source.encode()).hexdigest())
        self.assertEqual(record['accepted_by'], 'Project owner')
        packet = intent.attach({'budget': {'used_bytes': 100, 'limit_bytes': 20000}}, record)
        self.assertEqual(packet['intent']['path'], 'intents/task.md')
        self.assertGreater(packet['budget']['used_bytes'], 100)

    def test_missing_draft_duplicate_and_mismatched_intent_are_rejected(self):
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'A different request')
        self.path.write_text(self.source.replace('Status: accepted', 'Status: draft'))
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'Build the agreed feature')
        self.assertEqual(intent.load(self.root, 'Build the agreed feature', allow_draft=True)['status'], 'draft')
        self.path.write_text(self.source)
        (self.root / 'intents' / 'duplicate.md').write_text(self.source)
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'Build the agreed feature')

    def test_acceptance_without_digest_binds_on_first_guarded_use(self):
        """No paste step: the owner accepts, the first guarded run records the text."""
        self.path.write_text(self.unstamped)
        expected = hashlib.sha256(self.unstamped.encode()).hexdigest()
        preview = intent.load(self.root, 'Build the agreed feature')
        self.assertEqual((preview['acceptance_binding'], preview['accepted_digest']), ('unbound', expected))
        self.assertFalse((self.root / intent.LEDGER).exists(), 'loading alone must never record')
        bound = intent.bind(self.root, preview)
        self.assertEqual(bound['acceptance_binding'], 'first-use')
        ledger = json.loads((self.root / intent.LEDGER).read_text())
        self.assertEqual(ledger['intents']['intents/task.md']['digest'], expected)
        again = intent.load(self.root, 'Build the agreed feature')
        self.assertEqual((again['acceptance_binding'], again['accepted_digest']), ('recorded', expected))
        self.assertIs(intent.bind(self.root, again), again, 'a recorded acceptance is not re-bound')

    def test_edit_after_first_use_voids_acceptance_until_reaccepted(self):
        self.path.write_text(self.unstamped)
        intent.bind(self.root, intent.load(self.root, 'Build the agreed feature'))
        edited = self.unstamped.replace('The feature works for users.',
                                        'The feature works for users and drops the audit table.')
        self.path.write_text(edited)
        with self.assertRaises(intent.IntentBlocked) as caught:
            intent.load(self.root, 'Build the agreed feature')
        self.assertIn('changed after acceptance', str(caught.exception))
        self.assertIn(hashlib.sha256(edited.encode()).hexdigest(), str(caught.exception))
        # Adding an approval after acceptance is an edit like any other.
        self.path.write_text(self.unstamped + 'Approved sensitive changes: auth\n')
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'Build the agreed feature')
        # Re-acceptance (a new decision date) re-binds on the next guarded use.
        reaccepted = edited.replace('Accepted on: 2026-09-26', 'Accepted on: 2026-09-27')
        self.path.write_text(reaccepted)
        record = intent.load(self.root, 'Build the agreed feature')
        self.assertEqual(record['acceptance_binding'], 'unbound')
        intent.bind(self.root, record)
        self.assertEqual(intent.load(self.root, 'Build the agreed feature')['acceptance_binding'], 'recorded')
        # An explicit digest line also re-accepts, and wins over the record.
        self.path.write_text(self.stamp(edited))
        self.assertEqual(intent.load(self.root, 'Build the agreed feature')['acceptance_binding'], 'explicit')

    def test_bind_refuses_a_concurrent_different_binding(self):
        self.path.write_text(self.unstamped)
        stale = intent.load(self.root, 'Build the agreed feature')
        other = {**stale, 'accepted_digest': 'f' * 64}
        intent.bind(self.root, other)
        with self.assertRaises(intent.IntentBlocked) as caught:
            intent.bind(self.root, stale)
        self.assertIn('changed after acceptance', str(caught.exception))

    def test_unreadable_or_unknown_record_fails_closed(self):
        self.path.write_text(self.unstamped)
        ledger = self.root / intent.LEDGER
        ledger.parent.mkdir()
        for bad in ('{not json', '{"schema_version": 9, "intents": {}}', '[]'):
            with self.subTest(bad=bad):
                ledger.write_text(bad)
                with self.assertRaises(intent.IntentBlocked):
                    intent.load(self.root, 'Build the agreed feature')
        ledger.unlink()
        ledger.symlink_to(self.root / 'elsewhere.json')
        (self.root / 'elsewhere.json').write_text('{"schema_version": 1, "intents": {}}')
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'Build the agreed feature')

    def test_edit_after_acceptance_voids_it_until_reaccepted(self):
        edited = self.unstamped.replace('The feature works for users.',
                                        'The feature works for users and drops the audit table.')
        self.path.write_text(self.source.replace('The feature works for users.',
                                                 'The feature works for users and drops the audit table.'))
        with self.assertRaises(intent.IntentBlocked) as caught:
            intent.load(self.root, 'Build the agreed feature')
        self.assertIn('changed after acceptance', str(caught.exception))
        self.assertIn(hashlib.sha256(edited.encode()).hexdigest(), str(caught.exception))
        self.path.write_text(self.stamp(edited))
        self.assertEqual(intent.load(self.root, 'Build the agreed feature')['status'], 'accepted')

    def test_digest_must_be_single_and_inside_acceptance(self):
        digest = hashlib.sha256(self.unstamped.encode()).hexdigest()
        self.path.write_text(self.source + 'Accepted digest: ' + digest + '\n')
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'Build the agreed feature')
        moved = self.unstamped.replace('The feature is missing for users.\n',
                                       'The feature is missing for users.\nAccepted digest: X\n')
        self.path.write_text(moved)
        with self.assertRaises(intent.IntentBlocked) as caught:
            intent.load(self.root, 'Build the agreed feature')
        self.assertIn('not bound to its content', str(caught.exception))
        self.assertFalse((self.root / intent.LEDGER).exists())

    def test_sensitive_approvals_are_parsed_and_digest_bound(self):
        self.assertEqual(intent.load(self.root, 'Build the agreed feature')['approved_sensitive'], [])
        for value, expected in (('none', []), ('NONE', []), ('auth, Money', ['auth', 'money']),
                                ('schema,schema', ['schema'])):
            with self.subTest(value=value):
                self.path.write_text(self.stamp(self.unstamped + f'Approved sensitive changes: {value}\n'))
                self.assertEqual(intent.load(self.root, 'Build the agreed feature')['approved_sensitive'], expected)
        for bad in ('auth; rm -rf /', 'auth,', '../auth'):
            with self.subTest(bad=bad):
                self.path.write_text(self.stamp(self.unstamped + f'Approved sensitive changes: {bad}\n'))
                with self.assertRaises(intent.IntentBlocked):
                    intent.load(self.root, 'Build the agreed feature')
        twice = self.unstamped + 'Approved sensitive changes: auth\nApproved sensitive changes: money\n'
        self.path.write_text(self.stamp(twice))
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'Build the agreed feature')
        # Adding an approval after acceptance is an edit like any other.
        self.path.write_text(self.source + 'Approved sensitive changes: auth\n')
        with self.assertRaises(intent.IntentBlocked) as caught:
            intent.load(self.root, 'Build the agreed feature')
        self.assertIn('changed after acceptance', str(caught.exception))

    def test_blank_fields_never_borrow_the_next_line(self):
        blank_owner = self.unstamped.replace('Accepted by: Project owner', 'Accepted by:')
        self.path.write_text(self.stamp(blank_owner))
        with self.assertRaises(intent.IntentBlocked) as caught:
            intent.load(self.root, 'Build the agreed feature')
        self.assertIn('accepting owner', str(caught.exception))
        blank_approvals = self.unstamped + 'Approved sensitive changes:\n'
        self.path.write_text(self.stamp(blank_approvals))
        self.assertEqual(intent.load(self.root, 'Build the agreed feature')['approved_sensitive'], [])

    def test_draft_needs_no_digest(self):
        self.path.write_text(self.unstamped.replace('Status: accepted', 'Status: draft'))
        record = intent.load(self.root, 'Build the agreed feature', allow_draft=True)
        self.assertEqual((record['status'], record['accepted_digest'], record['acceptance_binding']),
                         ('draft', None, None))
        self.assertIs(intent.bind(self.root, record), record, 'a draft is never recorded')

    def test_unaccepted_fields_and_symlink_cannot_authorize_task(self):
        self.path.write_text(self.source.replace('Accepted by: Project owner', 'Accepted by: <owner name>'))
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'Build the agreed feature')
        self.path.unlink()
        self.path.symlink_to(self.root / 'outside.md')
        with self.assertRaises(intent.IntentBlocked):
            intent.load(self.root, 'Build the agreed feature')


if __name__ == '__main__':
    unittest.main()
