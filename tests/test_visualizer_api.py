"""Real Bun HTTP tests for prompt provenance, project projections and billing.

Requires Bun (KH_BUN may point at an existing executable). No dependency
installation, provider calls, or browser needed.
"""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
from contextlib import closing, contextmanager
import hashlib
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
TRACE_SCHEMA = ROOT / '.claude/skills/kh/templates/adws/adw_modules/trace-schema.sql'
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
        schema = TRACE_SCHEMA.read_text(encoding='utf-8')
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

    def review_ledger(self):
        folder = self.root / '.kh'
        folder.mkdir()
        snapshot = self.snapshot()
        definition = json.dumps(snapshot['tasks'][0])
        snapshot['tasks'][0].update(run_id='run-a', status='blocked', blocker='Old projection')
        (folder / 'project-status.json').write_text(json.dumps(snapshot))
        with closing(sqlite3.connect(folder / 'project.db')) as conn, conn:
            conn.executescript("""
                PRAGMA user_version=1;
                CREATE TABLE tasks(id TEXT PRIMARY KEY, definition TEXT, status TEXT, run_id TEXT,
                    summary TEXT, blocker TEXT, updated_at TEXT, changed_files TEXT);
                CREATE TABLE attempts(run_id TEXT PRIMARY KEY, task_id TEXT);
                CREATE TABLE events(sequence INTEGER PRIMARY KEY, task_id TEXT, run_id TEXT,
                    event TEXT, detail TEXT, created_at TEXT);
            """)
            conn.execute("INSERT INTO tasks VALUES ('t1', ?, 'blocked', 'run-b', '', 'Latest check failed', ?, '[]')",
                         (definition, '2026-01-03T10:00:00Z'))
            conn.execute("INSERT INTO tasks VALUES ('t2', ?, 'completed', 'run-c', '', NULL, ?, '[]')",
                         (json.dumps({'id': 't2'}), '2026-01-04T10:00:00Z'))
            conn.executemany("INSERT INTO attempts VALUES (?, ?)",
                             [('run-a', 't1'), ('run-b', 't1'), ('run-c', 't2')])
            conn.executemany("INSERT INTO events (task_id, run_id, event, detail, created_at) VALUES ('t1', ?, ?, ?, ?)", [
                ('run-a', 'running', 'null', '2026-01-01T10:00:00Z'),
                ('run-a', 'blocked', json.dumps('Owner review required'), '2026-01-01T11:00:00Z'),
                ('run-b', 'running', 'null', '2026-01-03T09:00:00Z'),
                ('run-b', 'blocked', json.dumps('Latest check failed'), '2026-01-03T10:00:00Z'),
            ])
        return hashlib.sha256(definition.encode()).hexdigest()

    def quality_phase(self, run, round_number, passed, tail):
        phase = f'{run}-checks-{round_number}'
        self.conn.execute("INSERT INTO phases (phase_id,adw_id,seq,name,kind,owner) VALUES (?,?,?,?,?,?)",
                          (phase, run, round_number * 2, f'checks_{round_number}', 'code', 'quality'))
        check = {'name': 'test', 'passed': passed, 'returncode': 0 if passed else 1, 'output_tail': tail}
        self.conn.execute("INSERT INTO events (event_id,adw_id,phase_id,type,name,payload_json) VALUES (?,?,?,?,?,?)",
                          (phase + '-log', run, phase, 'log', f'checks_{round_number}',
                           json.dumps({'passed': passed, 'checks': [check]})))
        self.conn.commit()

    def controller_patch(self, run, patch, *, listed=True):
        session = self.db_path.parent / 'sessions' / run
        handoff = session / 'context_handoff'
        handoff.mkdir(parents=True, exist_ok=True)
        path = handoff / 'controller_diff_1.patch'
        path.write_bytes(patch)
        (session / 'artifact-map.json').write_text(json.dumps({'version': 1, 'mappings': [{
            'owner_root': str(self.root),
            'published_handoff_files': [path.name] if listed else [],
        }]}))
        return path

    def review(self, base, run=None, task='t1'):
        query = '?' + urlencode({'run_id': run}) if run is not None else ''
        return self.request(base, f'/api/project/tasks/{task}/review{query}')

    def test_review_uses_ledger_history_and_evidence_from_exact_attempt_and_round(self):
        digest = self.review_ledger()
        patch = b'diff --kh a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-before\n+after\n'
        self.quality_phase('run-a', 1, True, 'held-check-output')
        self.quality_phase('run-b', 1, True, 'obsolete-check-output')
        self.quality_phase('run-b', 2, False, 'literal failing output')
        self.controller_patch('run-a', patch)
        self.controller_patch('run-b', b'Obsolete patch from first correction round')
        self.conn.execute("INSERT INTO phases (phase_id,adw_id,seq,name,kind,owner) VALUES (?,?,?,?,?,?)",
                          ('review-a', 'run-a', 3, 'review_1', 'agent', 'reviewer'))
        verdict = json.dumps({'approved': True, 'summary': 'Check and diff reviewed', 'blocking': [],
                              'findings': [{'requirement': 'Expected source', 'met': True, 'evidence': 'src/main.py'}]})
        self.conn.execute("INSERT INTO envelopes (envelope_id,adw_id,phase_id,agent,output_type,payload_json,valid) VALUES (?,?,?,?,?,?,?)",
                          ('review-envelope', 'run-a', 'review-a', 'reviewer', 'ReviewOutput', verdict, 1))
        self.conn.commit()
        ledger = self.root / '.kh' / 'project.db'
        original = ledger.read_bytes()
        with self.server() as base:
            code, data = self.review(base)
            self.assertEqual(200, code, data)
            self.assertEqual('run-b', data['task']['run_id'])
            self.assertEqual(digest, data['task_definition_sha256'])
            self.assertEqual(['run-b', 'run-a'], [item['run_id'] for item in data['attempts']])
            self.assertEqual('run-b', data['selected_attempt']['run_id'])
            self.assertEqual('Latest check failed', data['selected_attempt']['blocker'])
            self.assertEqual(2, data['phase']['round'])
            self.assertEqual('literal failing output', data['checks'][0]['output_tail'])
            self.assertIsNone(data['diff'])
            self.assertTrue(data['diff_unavailable'])
            code, old = self.review(base, 'run-a')
            self.assertEqual(200, code, old)
            self.assertEqual('Owner review required', old['selected_attempt']['blocker'])
            self.assertEqual('held-check-output', old['checks'][0]['output_tail'])
            self.assertEqual('Expected source', old['review']['findings'][0]['requirement'])
            self.assertEqual({'artifact': 'controller_diff_1.patch', 'round': 1,
                              'sha256': hashlib.sha256(patch).hexdigest(), 'text': patch.decode()}, old['diff'])
            self.assertEqual(404, self.review(base, 'run-c')[0])
            self.assertEqual(404, self.review(base, 'run-a', task='t2')[0])
            self.assertEqual(400, self.review(base, '../run-c')[0])
        self.assertEqual(original, ledger.read_bytes())

    def test_review_diff_line_numbers_bind_to_the_controller_hunk_and_side(self):
        patch = ('diff --kh a/src/path b/part.py b/src/path b/part.py\r\n'
                 '--- a/src/path b/part.py\r\n+++ b/src/path b/part.py\r\n'
                 '@@ -2,2 +7,2 @@\r\n value\r\n-old\r\n+new\r\n'
                 '\\ No newline at end of file\r\n'
                 'diff --kh a/blob b/blob\nbinary change\n')
        script = (f"import {{parseReviewDiff}} from {json.dumps(str(APP / 'shared/review-diff.ts'))};"
                  f"console.log(JSON.stringify(parseReviewDiff({json.dumps(patch)})));")
        result = subprocess.run([BUN, '-e', script], text=True, capture_output=True, check=True)
        files = json.loads(result.stdout)
        self.assertEqual(['src/path b/part.py', 'blob'], [file['path'] for file in files])
        self.assertEqual([
            {'kind': 'context', 'text': 'value', 'oldLine': 2, 'newLine': 7},
            {'kind': 'removed', 'text': 'old', 'oldLine': 3, 'newLine': None},
            {'kind': 'added', 'text': 'new', 'oldLine': None, 'newLine': 8},
        ], files[0]['lines'][3:6])
        self.assertEqual([{'kind': 'meta', 'text': 'binary change', 'oldLine': None, 'newLine': None}],
                         files[1]['lines'])

    def test_review_rejects_unpublished_oversized_unsafe_and_unmatched_diff(self):
        self.review_ledger()
        self.quality_phase('run-a', 1, True, 'Recorded test')
        patch = self.controller_patch('run-a', b'Unpublished content', listed=False)
        with self.server() as base:
            self.assertIsNone(self.review(base, 'run-a')[1]['diff'])
            self.controller_patch('run-a', b'x' * (1024 * 1024 + 1))
            self.assertIsNone(self.review(base, 'run-a')[1]['diff'])
            self.controller_patch('run-a', b'text\x00binary')
            self.assertIsNone(self.review(base, 'run-a')[1]['diff'])
            secret = self.root / 'secret.txt'
            secret.write_text('private content')
            patch.unlink()
            patch.symlink_to(secret)
            code, data = self.review(base, 'run-a')
            self.assertEqual(200, code, data)
            self.assertIsNone(data['diff'])
            self.assertNotIn('private content', json.dumps(data))
            patch.unlink()
            self.controller_patch('run-a', b'Changed proof')
            manifest_file = patch.parent.parent / 'artifact-map.json'
            manifest = json.loads(manifest_file.read_text())
            manifest['mappings'][0]['published_handoff_sha256'] = {
                patch.name: hashlib.sha256(b'Original proof').hexdigest(),
            }
            manifest_file.write_text(json.dumps(manifest))
            self.assertIsNone(self.review(base, 'run-a')[1]['diff'])
            self.controller_patch('run-a', b'Phase missing')
            self.conn.execute("DELETE FROM phases WHERE phase_id='run-a-checks-1'")
            self.conn.commit()
            self.assertIsNone(self.review(base, 'run-a')[1]['diff'])

    def test_projection_without_ledger_only_shows_projected_attempt(self):
        folder = self.root / '.kh'
        folder.mkdir()
        snapshot = self.snapshot()
        snapshot['tasks'][0].update(run_id='run-a', status='blocked', blocker='Projected blocker')
        (folder / 'project-status.json').write_text(json.dumps(snapshot))
        self.quality_phase('run-a', 1, True, 'Trace output')
        self.controller_patch('run-a', b'Source proof unavailable')
        with self.server() as base:
            code, data = self.review(base)
            self.assertEqual(200, code, data)
            self.assertFalse(data['history_available'])
            self.assertEqual('Projected blocker', data['selected_attempt']['blocker'])
            self.assertIsNone(data['task_definition_sha256'])
            self.assertIsNone(data['diff'])
            self.assertEqual(404, self.review(base, 'run-b')[0])

    def test_review_shows_the_held_round_categories_and_matching_intent(self):
        self.review_ledger()
        self.quality_phase('run-b', 1, True, 'Check passed')
        patch = self.controller_patch('run-b', b'Held diff')
        flags = patch.parent / 'owner_review_1.json'
        flags.write_text(json.dumps([{'category': 'auth', 'why': 'Changed authorization'}]))
        manifest_file = patch.parent.parent / 'artifact-map.json'
        manifest = json.loads(manifest_file.read_text())
        manifest['mappings'][0]['published_handoff_files'].append(flags.name)
        manifest_file.write_text(json.dumps(manifest))
        intents = self.root / 'intents'
        intents.mkdir()
        (intents / 'feature.md').write_text('## Task request\nBuild feature\n\n## Outcome\nA relevant change\n')
        (intents / 'other.md').write_text('## Task request\nDifferent feature\n')
        with closing(sqlite3.connect(self.root / '.kh' / 'project.db')) as conn, conn:
            conn.execute("UPDATE tasks SET blocker='OWNER_REVIEW_REQUIRED: auth' WHERE id='t1'")
        with self.server() as base:
            code, data = self.review(base, 'run-b')
            self.assertEqual(200, code, data)
            self.assertEqual({'categories': [{'category': 'auth', 'why': 'Changed authorization'}],
                              'intent_path': 'intents/feature.md'}, data['owner_hold'])
            self.assertIsNone(data['decision_draft'])
            self.assertIsNone(self.review(base, 'run-a')[1]['owner_hold'])

    def test_review_shows_only_the_verified_decision_draft_and_named_invocations(self):
        self.review_ledger()
        self.quality_phase('run-b', 1, True, 'Check passed')
        patch = self.controller_patch('run-b', b'Checked diff')
        candidate_digest = 'a' * 64
        artifact = 'controller_inputs/decision_draft-002.json'
        draft = patch.parent / artifact
        draft.parent.mkdir()
        draft.write_text(json.dumps({'schema_version': 1, 'in_force': False,
                                     'identity': {'candidate_sha256': candidate_digest},
                                     'findings': [{'text': 'Missing verification', 'category': 'owner_decision'}],
                                     'draft_decisions': [
                                         {'kind': 'historical_exception', 'task': 't1', 'invocations': ['run-b#92']},
                                         {'kind': 'historical_exception', 'task': 't2', 'invocations': ['run-c#91']},
                                     ]}))
        digest = hashlib.sha256(draft.read_bytes()).hexdigest()
        receipt = json.dumps({'schema_version': 1, 'run_id': 'run-b', 'files': {
            artifact: {'sha256': digest, 'kind': 'decision_draft', 'producer': 'controller'},
        }})
        receipt_digest = hashlib.sha256(receipt.encode()).hexdigest()[:12]
        receipt_path = f'controller_inputs/receipts/00000000000000000123-{receipt_digest}.json'
        (patch.parent / receipt_path).parent.mkdir()
        (patch.parent / receipt_path).write_text(receipt)
        manifest_file = patch.parent.parent / 'artifact-map.json'
        manifest = json.loads(manifest_file.read_text())
        manifest['mappings'][0]['published_handoff_files'].extend([artifact, receipt_path])
        manifest_file.write_text(json.dumps(manifest))
        current = self.root / '.kh' / 'work' / 't1' / 'current'
        current.mkdir(parents=True)
        (current / 'meta.json').write_text(json.dumps({
            'schema': 2, 'run_id': 'run-b', 'task': 't1',
            'checked': {'candidate_sha256': candidate_digest, 'verification_result': {'passed': True}},
        }))
        blocker = 'OWNER_DECISION_REQUIRED: a decision draft is at decision_draft-002.json in the controller inputs.'
        with closing(sqlite3.connect(self.root / '.kh' / 'project.db')) as conn, conn:
            conn.execute("UPDATE tasks SET blocker=? WHERE id='t1'", (blocker,))
        with self.server() as base:
            code, data = self.review(base, 'run-b')
            self.assertEqual(200, code, data)
            self.assertEqual(['run-b#92'], data['decision_draft']['invocations'])
            self.assertEqual(digest, data['decision_draft']['sha256'])
            self.assertTrue(data['decision_draft']['checked_candidate_recorded'])
            draft.write_text(draft.read_text().replace('run-b#92', 'run-b#99'))
            self.assertIsNone(self.review(base, 'run-b')[1]['decision_draft'])

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
