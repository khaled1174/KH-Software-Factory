"""Offline coverage for retained observer behavior; no provider calls or installs."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import concurrent.futures
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / '.claude/skills/task-observer/scripts'

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

store = load('observer_store', 'observer_store.py')

class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / '\u0645\u0634\u0631\u0648\u0639 with spaces'
        self.project.mkdir()
        (self.project / 'proof.txt').write_text('real test output\n')

    def record(self, project=None, **changes):
        fields = dict(title='failure', issue='client SECRET issue', suggestion='add guard',
                      principle='check errors', skill='sample', run_id='run-1', phase='build',
                      source_revision='abc123', evidence=['proof.txt'])
        fields.update(changes)
        return store.record_observation(project or self.project, **fields)

    def test_parallel_uuid_no_loss_and_dedup_preserves_occurrences(self):
        self.record(title='init')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda n: self.record(title=f'item {n}'), range(32)))
        self.assertEqual(32, len({r['id'] for r in records}))
        self.assertEqual(33, len(store.list_observations(self.project)))
        first = self.record(title='same')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            duplicates = list(pool.map(lambda n: self.record(title='same', run_id=f'run-{n}'), range(16)))
        self.assertTrue(all(r['id'] == first['id'] and r['deduplicated'] for r in duplicates))
        result = next(r for r in store.list_observations(self.project) if r['id'] == first['id'])
        self.assertEqual(17, len(result['occurrences']))

    def test_conflicting_status_update_rejected(self):
        r = self.record()
        self.assertEqual(2, store.set_status(self.project, r['id'], 'actioned', 1))
        with self.assertRaisesRegex(ValueError, 'conflict'):
            store.set_status(self.project, r['id'], 'declined', 1)
        self.assertEqual('actioned', store.list_observations(self.project)[0]['status'])

    def test_project_isolation_explicit_sanitized_promotion(self):
        r = self.record()
        other = self.base / 'other'; other.mkdir()
        self.assertEqual([], store.list_observations(other))
        with self.assertRaises(ValueError):
            store.promote_lesson(self.project, r['id'], self.base / 'shared', sanitized_lesson='lesson', reviewed_by='reviewer')
        path = store.promote_lesson(self.project, r['id'], self.base / 'shared',
                                    sanitized_lesson='Check command exit codes.', reviewed_by='reviewer', sanitization_confirmed=True)
        text = Path(path).read_text()
        self.assertNotIn('SECRET', text)
        self.assertNotIn(str(self.project), text)
        self.assertNotIn('proof.txt', text)
        self.assertEqual('Check command exit codes.', json.loads(text)['lesson'])

    def test_evidence_traversal_and_symlinks_rejected(self):
        (self.base / 'external.txt').write_text('external')
        with self.assertRaises(ValueError): self.record(evidence=['../external.txt'])
        (self.project / 'link').symlink_to(self.base / 'external.txt')
        with self.assertRaises(ValueError): self.record(evidence=['link'])

    def test_denial_not_retried_and_read_failure_not_empty(self):
        with mock.patch.object(store.sqlite3, 'connect', side_effect=PermissionError('denied')) as connect:
            with self.assertRaises(PermissionError): self.record()
            self.assertEqual(1, connect.call_count)
        folder = self.project / '.kh-observer'; folder.mkdir(exist_ok=True)
        (folder / 'observations.sqlite3').write_text('corrupt data')
        with self.assertRaises(sqlite3.DatabaseError): store.review_status(self.project)

    def test_date_fixtures_and_quoted_cli_unicode_path(self):
        for last in ('never', '2026-08-07', 'invalid', '2027-01-01'):
            self.assertTrue(store.review_due(last, 1, '2026-09-06')['offer_review'])
        self.assertFalse(store.review_due('2026-09-04', 1, '2026-09-06')['offer_review'])
        self.assertFalse(store.review_due('never', 0, '2026-09-06')['offer_review'])
        self.record()
        result = subprocess.run([sys.executable, str(SCRIPTS/'observer_store.py'), '--project', str(self.project),
                                 'review-status', '--today', '2026-09-06'], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, json.loads(result.stdout)['result']['open_count'])

    def test_core_compact_controller_only(self):
        text = (SCRIPTS.parent/'SKILL.md').read_text()
        self.assertLess(len(text.encode()), 4000)
        self.assertIn('primary controller', text)
        self.assertNotIn('for f in $(find', text)
        self.assertIn('permission denial ends that action', text)

    def test_retired_updates_cli_refuses_without_creating_storage(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS/'observer_store.py'), '--project', str(self.project), 'updates'],
            capture_output=True, text=True,
        )
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("invalid choice: 'updates'", result.stderr)
        self.assertFalse((self.project/'.kh-observer').exists())

    def test_core_storage_preserves_old_updates_without_creating_new_registry(self):
        folder = self.project/'.kh-observer'
        folder.mkdir()
        database = folder/'observations.sqlite3'
        old = ('old-proposal', 'reviewed', 3, '{"history": ["proposed", "staged", "reviewed"]}')
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE updates (id TEXT PRIMARY KEY, state TEXT, version INTEGER, payload TEXT)')
            db.execute('INSERT INTO updates VALUES (?, ?, ?, ?)', old)
            db.commit()
        snapshot = folder/'updates/old-proposal/candidate/SKILL.md'
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(b'old reviewed candidate\n')

        record = self.record()
        store.set_status(self.project, record['id'], 'actioned', record['version'])
        self.assertEqual('actioned', store.list_observations(self.project)[0]['status'])
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual([old], db.execute('SELECT * FROM updates').fetchall())
        self.assertEqual(b'old reviewed candidate\n', snapshot.read_bytes())

        fresh = self.base/'fresh project'; fresh.mkdir()
        self.record(project=fresh, evidence=[])
        self.assertEqual(1, len(store.list_observations(fresh)))
        with closing(sqlite3.connect(fresh/'.kh-observer/observations.sqlite3')) as db:
            self.assertEqual([], db.execute("SELECT name FROM sqlite_master WHERE name='updates'").fetchall())

if __name__ == '__main__': unittest.main()
