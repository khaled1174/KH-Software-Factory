"""Browser action requests through Bun and the guarded controller."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from tests import test_corrective_release as corrective
from tests import test_start as startup
from adw_modules import project

APP = startup.ROOT / '.claude/skills/kh/apps/visualizer'
BUN = os.environ.get('KH_BUN') or shutil.which('bun')
REPLY = 'Use the checked behavior for the empty input. Fixture correction 18.'


class ReviewActionFixture:
    setUp = startup.StartupTests.setUp
    tearDown = startup.StartupTests.tearDown
    init_plan = corrective.CorrectiveReleaseTests.init_plan
    tracked = corrective.CorrectiveReleaseTests.tracked

    def prepare(self):
        self.init_plan()
        self.entry = startup.ADWS / 'adw_start.py'
        shutil.copyfile(self.entry, self.root / 'adws/adw_start.py')
        self.launches = self.base / 'browser-launches.jsonl'
        self.cli.write_text(startup.FAKE.replace(
            "    if mode=='semantic':",
            "    reply_path=re.search(r'Read the scoped owner reply at (\\S+)\\.',user)\n"
            "    reply=json.loads(Path(reply_path[1]).read_text()).get('text') if reply_path else None\n"
            "    if mode=='browser-first' or (mode=='browser-feedback' and reply!=" + repr(REPLY) + "):\n"
            "        text='value = 3\\n'\n"
            "    if mode=='semantic':",
        ))
        launcher = self.base / 'uv'
        launcher.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'from pathlib import Path\n'
            "assert sys.argv[1:3] == ['run', 'adws/adw_start.py'], sys.argv\n"
            f'with Path({str(self.launches)!r}).open("a") as stream:\n'
            '    stream.write(json.dumps(sys.argv[3:]) + "\\n")\n'
            f'os.execv({sys.executable!r}, [{sys.executable!r}, "-B", {str(self.entry)!r}, *sys.argv[3:]])\n'
        )
        launcher.chmod(0o700)

    def seed_stop(self, run='stopped', reason='CHECKS_FAILED: fixture mismatch'):
        project.begin_task(self.root, 'T1', run)
        project.fail_task(self.root, 'T1', run, reason)

    def failed_checks(self, execution_mode='full'):
        first = self.tracked(run='failed-checks', mode='browser-first', extra=['--mode', execution_mode])
        if first.returncode == 0 or (self.root / 'src/app.py').read_text() != 'value = 1\n':
            raise RuntimeError(f'Expected a recorded failing candidate: {first.stdout}\n{first.stderr}')

    @contextmanager
    def server(self, *, enabled=True, port=0):
        if not port:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = {**self.env, 'KH_PROJECT_ROOT': str(self.root), 'PORT': str(port),
               'PATH': f"{self.base}{os.pathsep}{self.env['PATH']}",
               'KH_REVIEW_ACTIONS': '1' if enabled else '0', 'KH_FIXTURE_MODE': 'browser-feedback'}
        with tempfile.TemporaryFile(mode='w+') as logs:
            child = subprocess.Popen(
                [BUN, 'run', str(APP / 'server/index.ts'), '--db', str(self.root / 'adws/adw_data/kh.db')],
                cwd=self.root, env=env, stdout=logs, stderr=logs,
            )
            try:
                deadline = time.monotonic() + 10
                while child.poll() is None and time.monotonic() < deadline:
                    try:
                        if self.http(base, '/api/health')[0] == 200:
                            break
                    except (URLError, TimeoutError):
                        time.sleep(0.05)
                else:
                    logs.seek(0)
                    raise RuntimeError('Review server did not start. ' + logs.read())
                yield base
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)

    @staticmethod
    def http(base, path, *, data=None, token=None, origin=None):
        headers = {}
        if token is not None:
            headers['X-KH-Review-Capability'] = token
        payload = None
        if data is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': origin or base})
            payload = json.dumps(data).encode()
        request = Request(base + path, headers=headers, data=payload)
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def draft(self, base):
        _, review = self.http(base, '/api/project/tasks/T1/review')
        task = review['task']
        return {'request_id': str(uuid4()), 'task_id': 'T1', 'stopped_run_id': task['run_id'],
                'blocker': task['blocker'], 'task_definition_sha256': review['task_definition_sha256'],
                'action': 'request_changes', 'text': REPLY}

    def recorded_launches(self):
        return [json.loads(line) for line in self.launches.read_text().splitlines()] if self.launches.exists() else []


@unittest.skipUnless(BUN, 'Bun is unavailable')
class VisualizerReviewActionsTests(ReviewActionFixture, unittest.TestCase):
    def test_disabled_mode_and_wrong_origin_save_nothing(self):
        self.prepare()
        self.seed_stop()
        with self.server(enabled=False) as base:
            self.assertEqual(self.http(base, '/api/review/capability'), (200, {'enabled': False}))
            self.assertEqual(self.http(base, '/api/review/requests', data=self.draft(base))[0], 403)
        with self.server() as base:
            _, capability = self.http(base, '/api/review/capability')
            request = self.draft(base)
            self.assertEqual(self.http(base, '/api/review/requests', data=request, token=capability['token'],
                                       origin='http://unrelated.invalid')[0], 403)
        self.assertFalse((self.root / '.kh/review-requests').exists())
        self.assertEqual(self.recorded_launches(), [])

    def test_saved_draft_requires_preview_and_cannot_start_from_a_superseded_stop(self):
        self.prepare()
        self.seed_stop()
        with self.server() as base:
            token = self.http(base, '/api/review/capability')[1]['token']
            request = self.draft(base)
            payload = {'request_id': request['request_id'], 'confirm_saved_setup': True, 'setup_sha256': '0' * 64}
            self.assertEqual(self.http(base, '/api/review/requests', data=request, token=token)[0], 201)
            self.assertEqual(self.http(base, '/api/review/requests/start', data=payload, token=token)[0], 409)
            self.assertEqual(self.recorded_launches(), [])
            code, preview = self.http(base, '/api/review/requests/preview',
                                      data={'request_id': request['request_id']}, token=token)
            self.assertEqual(code, 200, preview)
            payload['setup_sha256'] = preview['setup_sha256']
            self.seed_stop('new-stop')
            self.assertEqual(self.http(base, '/api/review/requests/start', data=payload, token=token)[0], 409)
            saved = self.http(base, '/api/review/requests?id=' + request['request_id'], token=token)[1]
            self.assertEqual(saved['state'], 'saved')
            self.assertEqual(saved['request']['text'], REPLY)
            self.assertEqual(len(self.recorded_launches()), 1)
            self.assertIn('--dry-run', self.recorded_launches()[0])

    def test_incomplete_reservation_is_visible_after_server_restart_without_relaunch(self):
        self.prepare()
        self.seed_stop()
        with self.server() as base:
            token = self.http(base, '/api/review/capability')[1]['token']
            request = self.draft(base)
            self.assertEqual(self.http(base, '/api/review/requests', data=request, token=token)[0], 201)
        request_id = request['request_id']
        (self.root / '.kh/review-requests' / f'{request_id}.start.json.claim').touch()
        with self.server() as base:
            token = self.http(base, '/api/review/capability')[1]['token']
            state = self.http(base, '/api/review/requests/start', token=token,
                              data={'request_id': request_id, 'confirm_saved_setup': True, 'setup_sha256': '0' * 64})[1]
            self.assertEqual(state['state'], 'unknown')
            self.assertIn('interrupted', state['reason'])
            self.assertEqual(self.recorded_launches(), [])

    def test_browser_reply_reaches_builder_and_one_checked_attempt_is_accepted(self):
        self.prepare()
        self.failed_checks('light')
        with self.server() as base:
            token = self.http(base, '/api/review/capability')[1]['token']
            request = self.draft(base)
            request_id = request['request_id']
            stopped = self.http(base, '/api/project/tasks/T1/review')[1]
            self.assertIsNone(stopped['diff'])
            self.assertTrue(stopped['checks'], stopped)
            self.assertEqual(stopped['run_mode'], 'light')
            self.assertEqual(self.http(base, '/api/review/requests', data=request, token=token)[0], 201)
            code, preview = self.http(base, '/api/review/requests/preview', data={'request_id': request_id}, token=token)
            self.assertEqual(code, 200, preview)
            self.assertEqual(preview['mode'], 'light')
            payload = {'request_id': request_id, 'confirm_saved_setup': True, 'setup_sha256': preview['setup_sha256']}
            with ThreadPoolExecutor(max_workers=2) as requests:
                starts = list(requests.map(lambda _: self.http(base, '/api/review/requests/start', data=payload, token=token), range(2)))
            self.assertEqual(sorted(code for code, _ in starts), [200, 202], starts)
            run_id = starts[0][1]['run_id']
            self.assertTrue(run_id)
            self.assertEqual(starts[1][1]['run_id'], run_id)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                _, state = self.http(base, '/api/review/requests?id=' + request_id, token=token)
                if state['state'] in ('accepted', 'stopped', 'unknown'):
                    break
                time.sleep(0.15)
            self.assertEqual(state['state'], 'accepted', state)
            outcome_path = self.root / '.kh/review-requests' / f'{request_id}.outcome.json'
            self.assertEqual(json.loads(outcome_path.read_text())['exit_code'], 0)
            self.assertEqual((self.root / 'src/app.py').read_text(), 'value = 2\n')
            self.assertTrue(project.load_task(self.root, 'T1')['receipt']['review']['approved'])
            launches = [argv for argv in self.recorded_launches() if '--review-request' in argv]
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0][launches[0].index('--mode') + 1], 'light')

    def test_changed_setup_cannot_launch_the_previewed_attempt(self):
        self.prepare()
        self.seed_stop()
        with self.server() as base:
            token = self.http(base, '/api/review/capability')[1]['token']
            request = self.draft(base)
            request_id = request['request_id']
            self.assertEqual(self.http(base, '/api/review/requests', data=request, token=token)[0], 201)
            code, preview = self.http(base, '/api/review/requests/preview', data={'request_id': request_id}, token=token)
            self.assertEqual(code, 200, preview)
            self.cfg['routing']['default_profile'] = 'alternate'
            self.config.write_text(json.dumps(self.cfg))
            payload = {'request_id': request_id, 'confirm_saved_setup': True, 'setup_sha256': preview['setup_sha256']}
            self.assertEqual(self.http(base, '/api/review/requests/start', data=payload, token=token)[0], 202)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                _, state = self.http(base, '/api/review/requests?id=' + request_id, token=token)
                if state['state'] == 'stopped':
                    break
                time.sleep(0.1)
            self.assertEqual(state['state'], 'stopped', state)
            self.assertIn('the saved run setup changed after preview', state['reason'])
            self.assertFalse(state['recorded_run'])
            self.assertEqual(project.load_task(self.root, 'T1')['run_id'], 'stopped')
