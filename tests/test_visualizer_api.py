"""Real Bun HTTP tests for prompt provenance, project projections and billing.

Requires Bun (KH_BUN may point at an existing executable). No dependency
installation, provider calls, or browser needed.
"""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import ast
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / '.claude/skills/kh/apps/visualizer'
TRACER = ROOT / '.claude/skills/kh/templates/adws/adw_modules/tracer.py'
BUN = os.environ.get('KH_BUN') or shutil.which('bun')


@unittest.skipUnless(BUN, 'Bun is unavailable; set KH_BUN to run real HTTP integration tests')
class VisualizerApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project with spaces'
        self.root.mkdir()
        self.db_path = self.root / 'adws/adw_data/kh.db'
        self.db_path.parent.mkdir(parents=True)
        tree = ast.parse(TRACER.read_text())
        schema = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign) and any(
                          isinstance(target, ast.Name) and target.id == 'SCHEMA'
                          for target in node.targets))
        self.conn = sqlite3.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(schema)
        if 'cost_known' not in {row[1] for row in self.conn.execute('PRAGMA table_info(sessions)')}:
            self.conn.execute('ALTER TABLE sessions ADD COLUMN cost_known INTEGER')
        self.conn.execute("INSERT INTO sessions (adw_id,status,total_cost,cost_known) VALUES ('run-a','success',0,0)")
        self.conn.execute("INSERT INTO sessions (adw_id,status,total_cost,cost_known) VALUES ('run-b','success',1.25,1)")
        self.conn.execute("INSERT INTO phases (phase_id,adw_id,kind,owner) VALUES ('phase-a','run-a','agent','builder')")
        self.conn.execute("INSERT INTO phases (phase_id,adw_id,kind,owner) VALUES ('phase-b','run-b','agent','builder')")
        self.conn.commit()

    def prompt(self, phase='phase-a', *, legacy=False, system='system A', user='user A'):
        base = self.db_path.parent / 'sessions/run-a/builder'
        folder = base / 'prompts' if legacy else base / 'phases' / phase / 'prompts'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'system.md').write_text(system)
        (folder / 'user.md').write_text(user)
        return folder

    @contextmanager
    def server(self, *, db_path=None, root=None, app=None):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {key: value for key, value in os.environ.items()
               if key not in ('KH_DB', 'KH_PROJECT_ROOT')}
        env['PORT'] = str(port)
        if root is not None:
            env['KH_PROJECT_ROOT'] = str(root)
        log = tempfile.TemporaryFile(mode='w+')
        process = subprocess.Popen([BUN, 'run', str((app or APP) / 'server/index.ts'), '--db', str(db_path or self.db_path)],
                                   cwd=self.root, env=env, stdout=log, stderr=log)
        base = f'http://127.0.0.1:{port}'
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urlopen(base + '/api/health', timeout=.3) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    time.sleep(.05)
            else:
                self.fail('Bun HTTP server did not become healthy')
            if process.poll() is not None:
                log.seek(0)
                self.fail('Bun HTTP server exited: ' + log.read())
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            log.close()

    def request(self, base, path, method='GET'):
        try:
            response = urlopen(Request(base + path, method=method), timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def raw(self, base, path, method='GET', headers=None, body=None):
        try:
            response = urlopen(Request(base + path, method=method, headers=headers or {}, data=body), timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_default_single_port_serves_static_assets_and_refuses_foreign_hosts(self):
        recipe = (ROOT / '.claude/skills/kh/templates/justfile').read_text().split('\nobs:\n', 1)[1]
        self.assertIn('bun install --frozen-lockfile', recipe)
        self.assertIn('bun run build', recipe)
        self.assertIn('exec bun run server/index.ts', recipe)
        self.assertNotIn('bunx vite', recipe)
        app = self.root / 'visualizer'
        for folder in ('server', 'shared'):
            shutil.copytree(APP / folder, app / folder)
        (app / 'dist/assets').mkdir(parents=True)
        payloads = {'/': b'<html><body>Trace shell</body></html>',
                    '/assets/trace.js': b'console.log("trace asset")'}
        (app / 'dist/index.html').write_bytes(payloads['/'])
        (app / 'dist/assets/trace.js').write_bytes(payloads['/assets/trace.js'])
        with self.server(app=app) as base:
            port = base.rsplit(':', 1)[1]
            self.assertEqual(self.request(base, '/api/health')[0], 200)
            for path, expected in payloads.items():
                for host in ('localhost', '127.0.0.1'):
                    with self.subTest(path=path, host=host):
                        self.assertEqual(self.raw(base, path, headers={'Host': f'{host}:{port}'}), (200, expected))
                code, body = self.raw(base, path, headers={'Host': f'foreign.example:{port}'})
                self.assertEqual(code, 403, body)
                self.assertNotIn(expected, body)

    def test_closed_wal_database_starts_without_mutating_data(self):
        expected_dump = '\n'.join(self.conn.iterdump())
        self.conn.close()
        for suffix in ('-wal', '-shm'):
            self.assertFalse(Path(str(self.db_path) + suffix).exists())
        expected_bytes = self.db_path.read_bytes()
        with self.server() as base:
            code, health = self.request(base, '/api/health')
            self.assertEqual(200, code)
            self.assertTrue(health['db_ready'])
            code, sessions = self.request(base, '/api/sessions')
            self.assertEqual(200, code)
            self.assertEqual({'run-a', 'run-b'}, {row['adw_id'] for row in sessions})
            code, detail = self.request(base, '/api/sessions/run-a')
            self.assertEqual(200, code)
            self.assertEqual('success', detail['session']['status'])
            self.assertEqual(expected_bytes, self.db_path.read_bytes())
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(expected_dump, '\n'.join(conn.iterdump()))
        self.assertEqual(expected_bytes, self.db_path.read_bytes())

    def test_foreign_host_is_refused_on_every_route(self):
        """H6: DNS rebinding cannot read traces through a foreign Host name."""
        with self.server() as base:
            port = base.rsplit(':', 1)[1]
            for path in ('/api/sessions', '/api/sessions/run-a', '/api/health', '/api/project',
                         self.prompt_url(), '/api/nope', '/'):
                with self.subTest(path=path):
                    status, body = self.raw(base, path, headers={'Host': f'rebind.attacker.example:{port}'})
                    self.assertEqual(status, 403, body)
                    self.assertNotIn(b'run-a', body)
            for host in (f'localhost:{port}', f'127.0.0.1:{port}'):
                with self.subTest(host=host):
                    self.assertEqual(self.raw(base, '/api/sessions', headers={'Host': host})[0], 200)
            status, _ = self.raw(base, '/api/sessions/run-a/archive', 'POST',
                                 headers={'Host': f'rebind.attacker.example:{port}',
                                          'Content-Type': 'application/json'}, body=b'{"archived":true}')
            self.assertEqual(status, 403)

    def test_archive_requires_json_so_cross_site_posts_are_preflighted(self):
        with self.server() as base:
            status, _ = self.raw(base, '/api/sessions/run-a/archive', 'POST',
                                 headers={'Content-Type': 'text/plain'}, body=b'{"archived":true}')
            self.assertEqual(status, 415)
            self.assertEqual(self.conn.execute("SELECT archived FROM sessions WHERE adw_id='run-a'").fetchone()[0], 0)
            status, body = self.raw(base, '/api/sessions/run-a/archive', 'POST',
                                    headers={'Content-Type': 'application/json'}, body=b'{"archived":true}')
            self.assertEqual(status, 200, body)

    def prompt_url(self, phase='phase-a', run='run-a', agent='builder'):
        return f'/api/sessions/{run}/agents/{agent}/prompts?' + urlencode({'phase_id': phase})

    def test_prompts_use_requested_phase_and_reject_cross_session_or_agent(self):
        self.prompt()
        self.conn.execute("INSERT INTO phases (phase_id,adw_id,kind,owner) VALUES ('phase-c','run-a','agent','builder')")
        self.conn.commit()
        self.prompt('phase-c', system='system C', user='user C')
        with self.server() as base:
            code, data = self.request(base, self.prompt_url())
            self.assertEqual(200, code)
            self.assertEqual({'system': 'system A', 'user': 'user A', 'phase_id': 'phase-a'}, data)
            self.assertEqual('user C', self.request(base, self.prompt_url('phase-c'))[1]['user'])
            self.assertEqual(404, self.request(base, self.prompt_url('phase-b'))[0])
            self.assertEqual(404, self.request(base, self.prompt_url(agent='reviewer'))[0])
            self.assertEqual(400, self.request(base, '/api/sessions/run-a/agents/builder/prompts')[0])

    def test_missing_modern_phase_does_not_fall_back_to_legacy(self):
        self.prompt(legacy=True, user='wrong legacy ask')
        modern = self.prompt()
        (modern / 'user.md').unlink()
        with self.server() as base:
            code, data = self.request(base, self.prompt_url())
            self.assertEqual(200, code)
            self.assertEqual('system A', data['system'])
            self.assertIsNone(data['user'])

    def test_unambiguous_legacy_prompt_remains_readable(self):
        self.prompt(legacy=True)
        with self.server() as base:
            self.assertEqual('user A', self.request(base, self.prompt_url())[1]['user'])
            self.assertEqual('user A', self.request(base, '/api/sessions/run-a/agents/builder/prompts')[1]['user'])

    def test_prompt_traversal_and_symlink_are_rejected(self):
        folder = self.prompt()
        secret = self.root / 'outside.md'
        secret.write_text('do not expose')
        (folder / 'user.md').unlink()
        (folder / 'user.md').symlink_to(secret)
        with self.server() as base:
            self.assertEqual(400, self.request(base, self.prompt_url('../outside'))[0])
            code, data = self.request(base, self.prompt_url())
            self.assertEqual(400, code)
            self.assertNotIn('do not expose', json.dumps(data))

    def snapshot(self):
        return {'schema_version': 1, 'project': {'id': 'demo', 'name': 'Demo'},
                'totals': {'tasks': 1, 'completed': 0, 'pending': 1, 'active': 0,
                           'blocked': 0, 'completion_percent': 0},
                'milestones': [{'id': 'm1', 'title': 'First milestone', 'total': 1,
                                'completed': 0, 'status': 'pending', 'completion_percent': 0}],
                'tasks': [{'id': 't1', 'milestone_id': 'm1', 'title': 'First task',
                           'request': 'Build feature', 'dependencies': [], 'allowed_paths': ['src/**'],
                           'weight': 1, 'status': 'pending'}], 'next_tasks': ['t1']}

    def test_project_absence_is_honest_and_read_only(self):
        with self.server() as base:
            code, data = self.request(base, '/api/project')
            self.assertEqual(200, code)
            self.assertFalse(data['available'])
            self.assertIn('kh-project', data['reason'])
            self.assertFalse((self.root / '.kh').exists())

    def test_project_is_visible_before_first_run_and_database_attaches_later(self):
        folder = self.root / '.kh'
        folder.mkdir()
        (folder / 'project-status.json').write_text(json.dumps(self.snapshot()))
        future_db = self.root / 'future.db'
        with self.server(db_path=future_db, root=self.root) as base:
            self.assertFalse(self.request(base, '/api/health')[1]['db_ready'])
            self.assertTrue(self.request(base, '/api/project')[1]['available'])
            self.assertEqual([], self.request(base, '/api/sessions')[1])
            self.assertFalse(future_db.exists())
            with closing(sqlite3.connect(future_db)) as dest:
                self.conn.backup(dest)
                dest.commit()
            for suffix in ('-wal', '-shm'):
                self.assertFalse(Path(str(future_db) + suffix).exists())
            self.assertEqual(2, len(self.request(base, '/api/sessions')[1]))
            self.assertTrue(self.request(base, '/api/health')[1]['db_ready'])

    def test_project_snapshot_is_read_only_and_custom_database_needs_root(self):
        folder = self.root / '.kh'
        folder.mkdir()
        file = folder / 'project-status.json'
        text = json.dumps(self.snapshot())
        file.write_text(text)
        with self.server() as base:
            code, data = self.request(base, '/api/project')
            self.assertEqual(200, code)
            self.assertTrue(data['available'])
            self.assertEqual(['t1'], data['next_tasks'])
            self.assertNotEqual(200, self.request(base, '/api/project', 'POST')[0])
        self.assertEqual(text, file.read_text())
        custom = self.root / 'custom.db'
        with closing(sqlite3.connect(custom)) as dest:
            self.conn.backup(dest)
            dest.commit()
        with self.server(db_path=custom) as base:
            self.assertFalse(self.request(base, '/api/project')[1]['available'])
        with self.server(db_path=custom, root=self.root) as base:
            self.assertTrue(self.request(base, '/api/project')[1]['available'])

    def test_project_corruption_and_symlink_do_not_become_empty_success(self):
        folder = self.root / '.kh'
        folder.mkdir()
        file = folder / 'project-status.json'
        file.write_text('{broken')
        with self.server() as base:
            self.assertEqual(422, self.request(base, '/api/project')[0])
            file.unlink()
            other = self.root / 'external.json'
            other.write_text(json.dumps(self.snapshot()))
            file.symlink_to(other)
            self.assertEqual(400, self.request(base, '/api/project')[0])

    def test_cost_known_new_and_old_database(self):
        with self.server() as base:
            rows = {r['adw_id']: r for r in self.request(base, '/api/sessions')[1]}
            self.assertEqual(0, rows['run-a']['cost_known'])
            self.assertEqual(1, rows['run-b']['cost_known'])
            self.assertEqual(1.25, rows['run-b']['total_cost'])
        self.conn.execute('ALTER TABLE sessions DROP COLUMN cost_known')
        self.conn.commit()
        with self.server() as base:
            self.assertIsNone(self.request(base, '/api/sessions/run-a')[1]['session']['cost_known'])

    def test_unknown_cost_formatting_preserves_subtotal_without_false_zero(self):
        script = (f"import {{fmtKnownCost}} from {json.dumps(str(APP / 'src/lib/format.ts'))};"
                  "console.log(JSON.stringify([fmtKnownCost(0,false),fmtKnownCost(0,null),"
                  "fmtKnownCost(1.25,false),fmtKnownCost(0,true),fmtKnownCost(1.25,1)]));")
        result = subprocess.run([BUN, '-e', script], text=True, capture_output=True, check=True)
        self.assertEqual(['Unknown', 'Unknown', 'Partial $1.25', '$0.0000', '$1.25'],
                         json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
