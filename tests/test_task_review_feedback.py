"""Guarded intake of a reply to an exact stopped attempt."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_corrective_release as corrective
from tests import test_start as startup
from adw_modules import project, session


class TaskReviewFeedbackTests(unittest.TestCase):
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    unchanged = startup.StartupTests.unchanged
    init_plan = corrective.CorrectiveReleaseTests.init_plan
    tracked = corrective.CorrectiveReleaseTests.tracked

    def stopped(self, mode='semantic'):
        self.init_plan()
        result = self.tracked(mode=mode)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.unchanged()
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'blocked')

    def reply(self, **updates):
        request_id = str(uuid4())
        task = project.load_task(self.root, 'T1')
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            definition, = conn.execute("SELECT definition FROM tasks WHERE id='T1'").fetchone()
        data = {'schema_version': 1, 'request_id': request_id, 'task_id': 'T1',
                'stopped_run_id': task['run_id'], 'task_definition_sha256': sha256(definition.encode()).hexdigest(),
                'blocker': task['blocker'], 'action': 'request_changes',
                'text': 'Remove the unrelated flag and leave the value.', 'created_at': '2026-11-20T12:00:00Z',
                **updates}
        path = self.root / '.kh/review-requests' / f'{request_id}.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def start(self, path, *, run='browser-feedback', mode='good'):
        return self.tracked(run=run, mode=mode, extra=['--review-request', path.stem])

    def session_dir(self, run):
        return self.root / 'adws/adw_data/sessions' / run

    def anchored_reply(self, **updates):
        patch = sorted(self.session_dir('tracked').glob('context_handoff/controller_diff_*.patch'))[-1]
        anchor = {'artifact': patch.name, 'patch_sha256': sha256(patch.read_bytes()).hexdigest(),
                  'path': 'src/app.py', 'side': 'new', 'line': 2,
                  'line_sha256': sha256(b'unrelated_feature = True').hexdigest(), **updates}
        return self.reply(anchor=anchor), patch

    def test_anchored_reply_reaches_guarded_build_and_records_exact_request(self):
        self.stopped()
        path, _ = self.anchored_reply()
        builder = r"""elif role=='builder':
    if mode == 'read-owner-reply':
        source = re.search(r'Read the scoped owner reply at (.+?)\. It guides work', user)[1]
        reply = json.loads(Path(source).read_text())
        assert reply['text'] == 'Remove the unrelated flag and leave the value.'
        assert reply['anchor']['line'] == 2
        assert not Path('.kh/review-requests').exists()
"""
        self.cli.write_text(startup.FAKE.replace("elif role=='builder':\n", builder))
        result = self.start(path, mode='read-owner-reply')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.session_dir('browser-feedback')
        prompt = next(trace.glob('builder/phases/*/attempt-001/user.md')).read_text()
        self.assertIn('Read the scoped owner reply at ', prompt)
        self.assertIn('owner-review-request.json', prompt)
        artifact = 'controller_inputs/owner-review-request.json'
        recorded_bytes = (trace / 'context_handoff' / artifact).read_bytes()
        record = json.loads(recorded_bytes)
        maps = json.loads((trace / 'artifact-map.json').read_text())['mappings']
        self.assertEqual(maps[-1]['published_handoff_sha256'][artifact], sha256(recorded_bytes).hexdigest())
        self.assertEqual(record['request_sha256'], sha256(path.read_bytes()).hexdigest())
        self.assertEqual(record['anchor']['line'], 2)
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'completed')
        self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')
        with closing(sqlite3.connect(self.root / '.kh/project.db')) as conn:
            running, = conn.execute("SELECT detail FROM events WHERE run_id=? AND event='running'",
                                    ('browser-feedback',)).fetchone()
        self.assertEqual(json.loads(running)['review_request_id'], path.stem)
        repeated = self.start(path, run='browser-repeat')
        self.assertIn('REVIEW_REQUEST_STALE', repeated.stderr)
        self.assertFalse(self.session_dir('browser-repeat').exists())

    def test_replaced_patch_and_busy_lease_never_start_the_attempt(self):
        self.stopped()
        path, patch = self.anchored_reply()
        with session.acquire_repo_lock(self.root):
            self.assertIn('BUSY:', self.start(path).stderr)
        patch.write_bytes(patch.read_bytes() + b'\n')
        result = self.start(path)
        self.assertIn('REVIEW_REQUEST_STALE: the published patch changed', result.stderr)
        self.assertFalse(self.session_dir('browser-feedback').exists())
        self.unchanged()

    def test_changed_kept_file_stops_before_a_builder_and_leaves_candidate_evidence(self):
        self.stopped()
        path, _ = self.anchored_reply()
        kept = self.root / '.kh/work/T1/current'
        metadata = (kept / 'meta.json').read_bytes()
        (kept / 'files/src/app.py').write_text('value = 99\n')
        result = self.start(path)
        self.assertIn('REVIEW_REQUEST_STALE: the selected line', result.stderr)
        self.assertEqual(list(self.session_dir('browser-feedback').glob('builder/phases/*')), [])
        self.assertEqual((kept / 'meta.json').read_bytes(), metadata)
        self.assertEqual((kept / 'files/src/app.py').read_text(), 'value = 99\n')
        self.unchanged()

    def test_line_outside_diff_is_rejected(self):
        self.stopped()
        path, _ = self.anchored_reply(line=100)
        result = self.start(path)
        self.assertIn('REVIEW_REQUEST_STALE: the selected line', result.stderr)
        self.assertEqual(list(self.session_dir('browser-feedback').glob('builder/phases/*')), [])
        self.unchanged()

    def test_task_level_retry_still_checks_and_review_can_stop_it(self):
        self.stopped(mode='crash-build')
        path = self.reply(action='retry', text='')
        result = self.start(path, mode='semantic')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.session_dir('browser-feedback')
        self.assertTrue(list(trace.glob('reviewer/phases/*')))
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'blocked')
        self.unchanged()

    def test_owner_decisions_and_stale_definitions_do_not_dispatch(self):
        self.stopped(mode='crash-build')
        path = self.reply(task_definition_sha256='0' * 64)
        self.assertIn('REVIEW_REQUEST_STALE', self.start(path).stderr)
        project.block_task(self.root, 'T1', 'OWNER_REVIEW_REQUIRED: fixture hold')
        path = self.reply()
        self.assertIn('existing owner decision', self.start(path).stderr)
        self.assertFalse(self.session_dir('browser-feedback').exists())
        self.unchanged()

    def test_setup_confirmed_in_preview_binds_the_roles_at_start(self):
        self.stopped(mode='crash-build')
        path = self.reply()
        preview = self.tracked(extra=['--dry-run'])
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        digest = json.loads(preview.stdout)['review_setup_sha256']
        configuration = self.config.read_bytes()
        next(agent for agent in self.cfg['agents'] if agent['name'] == 'builder')['thinking'] = 'low'
        self.config.write_text(json.dumps(self.cfg))
        with patch.dict(self.env, KH_REVIEW_SETUP_SHA256=digest):
            stale = self.start(path)
            self.assertIn('REVIEW_REQUEST_STALE: the saved run setup changed after preview', stale.stderr)
            self.assertFalse(self.session_dir('browser-feedback').exists())
            self.unchanged()
            self.config.write_bytes(configuration)
            accepted = self.start(path)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(project.load_task(self.root, 'T1')['status'], 'completed')
