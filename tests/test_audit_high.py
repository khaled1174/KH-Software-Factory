"""Regression tests for the High findings of docs/AUDIT_2026-10-08.md (H1-H9)."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADWS = ROOT / '.claude/skills/kh/templates/adws'
sys.path.insert(0, str(ADWS))
from adw_modules import agent_cc
from adw_modules.agent_cc import CCRequest
from adw_modules.process_io import StreamRequest, run_stream


def cc_request(prompt, tools=('read', 'bash')):
    return CCRequest(prompt=prompt, system_prompt='sys', model='sonnet', session_id='id',
                     session_dir='s', raw_output_path='o', tools=None if tools is None else list(tools))


class H1ClaudeArgvTests(unittest.TestCase):
    """`--tools` is variadic in the Claude Code CLI; the prompt must follow `--`."""

    def test_prompt_follows_double_dash_for_every_tools_shape(self):
        for tools in (('read', 'bash'), (), None):
            with self.subTest(tools=tools):
                cmd = agent_cc._command(cc_request('Reply with PONG', tools), False)
                self.assertEqual(cmd[-2:], ['--', 'Reply with PONG'])
                self.assertEqual(cmd.count('--'), 1)

    def test_dash_leading_prompt_is_not_an_option(self):
        cmd = agent_cc._command(cc_request('- fix the failing test'), False)
        self.assertEqual(cmd[-2:], ['--', '- fix the failing test'])
        self.assertGreater(cmd.index('--'), cmd.index('--tools'))


class H9LargePromptTests(unittest.TestCase):
    """One argv string is capped at 128 KiB on Linux; large prompts go via stdin."""

    def test_large_prompt_leaves_argv_and_small_prompt_stays(self):
        small, large = cc_request('x' * 1000), cc_request('é' * (agent_cc.PROMPT_ARGV_LIMIT // 2 + 1))
        self.assertFalse(agent_cc.prompt_via_stdin(small))
        self.assertTrue(agent_cc.prompt_via_stdin(large))   # counted in UTF-8 bytes
        cmd = agent_cc._command(large, False)
        self.assertNotIn('--', cmd)
        self.assertTrue(all(len(a.encode()) < agent_cc.PROMPT_ARGV_LIMIT for a in cmd))

    def test_stream_runner_delivers_large_stdin_without_deadlock(self):
        text = 'line\n' * 400_000   # 2 MB, far above pipe buffers on both sides
        child = ('import sys\ndata=sys.stdin.read()\n'
                 'sys.stdout.write("x"*1_000_000+"\\n")\nprint(len(data))\n')
        out = []
        rc, err = run_stream(StreamRequest([sys.executable, '-c', child], tempfile.gettempdir(),
                                           dict(os.environ), 60, input_text=text), out.append)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out[-1].strip(), str(len(text)))

    def test_without_input_the_child_sees_end_of_file(self):
        out = []
        rc, _ = run_stream(StreamRequest([sys.executable, '-c', 'import sys;print(repr(sys.stdin.read()))'],
                                         tempfile.gettempdir(), dict(os.environ), 30), out.append)
        self.assertEqual((rc, out[-1].strip()), (0, "''"))


class H2BoundedEnvironmentTests(unittest.TestCase):
    """Every child an agent or gate can influence gets operator_env(), never os.environ."""

    def setUp(self):
        from adw_modules import utils
        self.utils = utils
        from unittest.mock import patch
        self.addCleanup(utils.set_dotenv_allowlist, None)
        patcher = patch.object(utils, 'DOTENV_KEYS', frozenset({'KH_H2_SECRET'}))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {'KH_H2_SECRET': 'sk_live_fixture'})
        env.start()
        self.addCleanup(env.stop)
        utils.set_dotenv_allowlist([])

    def test_tests_pass_gate_child_does_not_see_withheld_secret(self):
        from types import SimpleNamespace
        from adw_modules import gates
        probe = [sys.executable, '-c', 'import os,sys; sys.exit(3 if "KH_H2_SECRET" in os.environ else 0)']
        report = gates.tests_pass(probe)(None, SimpleNamespace(repo_root=tempfile.gettempdir()))
        self.assertTrue(report.passed, report.model_dump())

    @unittest.skipUnless(__import__('shutil').which('node'), 'Node required for the real relay')
    def test_delegate_relay_and_codex_do_not_see_withheld_secret(self):
        from tests import test_delegate_adapter as delegate
        fixture = delegate.DelegateTests('test_real_relay_subprocess_and_failure_cases')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        env = fixture._fake_cli()
        codex = fixture.base / 'bin/codex'
        seen = fixture.base / 'seen.txt'
        codex.write_text(codex.read_text().replace(
            'pathlib.Path(os.environ["KH_TEST_ARGS"])',
            f'pathlib.Path({str(seen)!r}).write_text(os.environ.get("KH_H2_SECRET", "<absent>"))\n'
            'pathlib.Path(os.environ["KH_TEST_ARGS"])', 1))
        from unittest.mock import patch
        from adw_modules.agent_delegate import run
        with patch.dict(os.environ, env):
            result = run(fixture.request)
        self.assertEqual(result.returncode, 0, result.reported_error)
        self.assertEqual(seen.read_text(), '<absent>')


class H3LauncherPreloadedDotenvTests(unittest.TestCase):
    """A launcher that exports .env first (just `set dotenv-load`) must not disable the boundary."""

    def keys(self, extra_env):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / '.env').write_text('KH_H3_KEY=from-file\n')
            import shutil
            shutil.copytree(ADWS / 'adw_modules', project / 'adws/adw_modules')
            env = {k: v for k, v in os.environ.items() if not k.startswith('KH_H3')}
            env.update(extra_env)
            out = subprocess.run([sys.executable, '-B', '-c',
                                  'import sys; sys.path.insert(0, "adws"); from adw_modules import utils; '
                                  'print("KH_H3_KEY" in utils.DOTENV_KEYS)'],
                                 cwd=project, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.strip()

    def test_file_value_counts_as_file_sourced_however_it_arrived(self):
        self.assertEqual(self.keys({}), 'True')                                  # Python loaded it
        self.assertEqual(self.keys({'KH_H3_KEY': 'from-file'}), 'True')        # launcher preloaded it
        self.assertEqual(self.keys({'KH_H3_KEY': 'operator-own'}), 'False')    # operator's own value

    def test_parent_dotenv_does_not_enter_project_or_child_environment(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            project = parent / 'project'
            project.mkdir()
            (parent / '.env').write_text('KH_H3_PARENT_ONLY=parent-fixture\n')
            shutil.copytree(ADWS / 'adw_modules', project / 'adws/adw_modules')
            env = {k: v for k, v in os.environ.items() if not k.startswith('KH_H3')}
            code = ('import sys,os,json; sys.path.insert(0,"adws"); '
                    'from adw_modules import utils; '
                    'print(json.dumps(["KH_H3_PARENT_ONLY" in os.environ, '
                    '"KH_H3_PARENT_ONLY" in utils.operator_env(), '
                    '"KH_H3_PARENT_ONLY" in utils.DOTENV_KEYS]))')
            result = subprocess.run([sys.executable, '-B', '-c', code], cwd=project,
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [False, False, False])




class H4DirectoryChangesTests(unittest.TestCase):
    """A directory kind or mode change is owner state: scoped, reviewed and diffed like a file."""

    def setUp(self):
        from tests import test_start
        self.fixture = test_start.StartupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_structural_rule(self):
        from adw_modules.permissions import Entry, structural_directory
        snap = lambda entries: type('S', (), {'entries': entries})()
        d, f = Entry('directory', 0o755, b''), Entry('file', 0o644, b'x')
        self.assertTrue(structural_directory('a', snap({}), snap({'a': d})))                     # created
        self.assertTrue(structural_directory('a', snap({'a': d}), snap({})))                     # removed
        self.assertFalse(structural_directory('a', snap({'a': f}), snap({'a': d})))              # kind change
        self.assertFalse(structural_directory('a', snap({'a': d}),
                                              snap({'a': Entry('directory', 0o777, b'')})))      # mode change

    def test_out_of_scope_file_turned_into_directory_is_not_promoted(self):
        result = self.fixture.invoke('dir-kind')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.fixture.unchanged()   # private.txt is still the owner's file, src/app.py untouched

    def test_out_of_scope_directory_mode_change_is_not_promoted(self):
        root = self.fixture.root
        before = (root / 'src').stat().st_mode & 0o777
        result = self.fixture.invoke('dir-mode')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'src').stat().st_mode & 0o777, before)
        self.fixture.unchanged()


class H5TraceDatabaseTests(unittest.TestCase):
    """The live trace DB is controller state; no agent write allowlist reaches it."""

    def test_trace_db_and_wal_files_are_never_writable(self):
        from types import SimpleNamespace
        from adw_modules.data_types import KHConfig
        from adw_modules.permissions import permitted
        builder = SimpleNamespace(writes=None)
        for db in ('adws/adw_data/kh.db', './custom/trace.db'):
            cfg = KHConfig.model_validate({'observability': {'db': db}})
            target = db.removeprefix('./')
            for suffix in ('', '-wal', '-shm'):
                with self.subTest(db=db, suffix=suffix):
                    self.assertFalse(permitted(target + suffix, builder, cfg))
                    self.assertFalse(permitted(target + suffix, SimpleNamespace(writes=['**']), cfg))
        self.assertTrue(permitted('src/app.py', builder, KHConfig()))


class H7PackageLocalStateTests(unittest.TestCase):
    """The release archive never contains credentials or local controller state."""

    def test_local_state_is_never_a_member(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('kh_package_h7', ROOT / 'scripts/package.py')
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        local = ('.env', 'sub/.env', '.env.production', '.claude/settings.local.json', 'adws/adw_data/kh.db',
                 'adws/adw_data/sessions/x/acceptance.json', '.kh/project.db', 'trace.sqlite3-wal',
                 '.kh-observer/obs.jsonl', 'a/b.pyc')
        shipped = ('.env.example', 'README.md', '.claude/settings.json',
                   '.claude/skills/kh/templates/adws/adw_start.py', '.claude/skills/kh/templates/env.sample')
        for rel in local:
            with self.subTest(rel=rel):
                self.assertTrue(package.local_state(Path(rel)))
        for rel in shipped:
            with self.subTest(rel=rel):
                self.assertFalse(package.local_state(Path(rel)))

    def test_planted_env_does_not_reach_the_zip(self):
        import shutil, zipfile
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / 'kh-software-factory'
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns('__pycache__', 'node_modules', 'dist'))
            (copy / '.env').write_text('ANTHROPIC_API_KEY=sk-ant-fixture\n')
            (copy / 'adws/adw_data').mkdir(parents=True, exist_ok=True)
            (copy / 'adws/adw_data/kh.db').write_bytes(b'SQLite format 3\x00')
            (copy / '.kh').mkdir(exist_ok=True)
            (copy / '.kh/project.db').write_bytes(b'local project state')
            out = Path(temporary) / 'x.zip'
            done = subprocess.run([sys.executable, '-B', str(copy / 'scripts/package.py'), '--output', str(out),
                                   '--skip-verify'], capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            prefix = 'not packaged (local state): '
            excluded = set(next(line for line in done.stderr.splitlines() if line.startswith(prefix))
                           .removeprefix(prefix).split(', '))
            self.assertTrue({'.env', '.kh/project.db', 'adws/adw_data/kh.db'} <= excluded, excluded)
            with zipfile.ZipFile(out) as bundle:
                names = bundle.namelist()
                self.assertFalse([n for n in names if n.endswith('/.env') or 'adw_data/' in n or '/.kh/' in n])
                manifest = bundle.read('kh-software-factory/MANIFEST.sha256').decode()
            self.assertNotIn('.env\n', manifest)
            self.assertNotIn('.kh/project.db\n', manifest)


class H8InclusiveMatchingTests(unittest.TestCase):
    """Adapter and light-mode risk rules must never under-select (also M6)."""

    def test_double_star_patterns_select_root_and_nested_paths(self):
        from adw_modules.verification_evidence import affected_surfaces
        self.assertEqual(affected_surfaces(['src/app.py', 'app.py', 'docs/a.md'], ['src/**/*.py']), ['src/app.py'])
        self.assertEqual(affected_surfaces(['src/app.py', 'app.py', 'docs/a.md'], ['**/*.py']), ['app.py', 'src/app.py'])
        self.assertEqual(affected_surfaces(['src/a/b.py'], ['src/*.py']), ['src/a/b.py'])  # historic fnmatch kept
        self.assertEqual(affected_surfaces(['web/x/y.ts'], ['web/**/']), ['web/x/y.ts'])

    def test_light_risk_rules_cover_root_level_and_directory_patterns(self):
        from types import SimpleNamespace
        from adw_modules.data_types import KHConfig
        from adw_modules.execution_policy import validate_light_paths
        light = SimpleNamespace(mode='light')
        for rules, path in ((['**/migrations/**'], 'migrations/0001.py'), (['db/migrations/'], 'db/migrations/1.sql'),
                            (['**/migrations/'], 'app/migrations/x.py')):
            with self.subTest(rules=rules, path=path):
                cfg = KHConfig.model_validate({'execution': {'light_risk_paths': rules}})
                with self.assertRaisesRegex(ValueError, 'REQUIRES_FULL_MODE: path matches owner light-mode risk'):
                    validate_light_paths(cfg, light, [path])
        validate_light_paths(KHConfig.model_validate({'execution': {'light_risk_paths': ['**/migrations/**']}}),
                             light, ['src/app.py'])


class H9MediumRepositoryTests(unittest.TestCase):
    """A bounded directory map is delivered once, and large prompts use stdin.

    The map no longer lists all 2,400 files. A fixture-only template tail keeps
    the rendered prompt above Linux's per-argument limit, independently of map
    compression, so the original E2BIG transport regression remains covered.
    """
    FILES = 2400

    def test_guarded_start_completes_and_planner_prompt_carries_the_packet_once(self):
        from tests import test_start
        fixture = test_start.StartupTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        template = fixture.root / 'adws/adw_kh_config/user.md'
        template.write_text(template.read_text() + '\n' + 'Transport threshold fixture padding.\n' * 4000)
        for i in range(self.FILES):
            path = fixture.root / f'src/pkg{i // 50:02d}/module_{i:04d}.py'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'x = {i}\n')
        for args in (['add', '.'], ['-c', 'user.name=F', '-c', 'user.email=f@example.invalid', 'commit', '-qm', 'more']):
            subprocess.run(['git', '-C', str(fixture.root), *args], check=True, capture_output=True)
        result = fixture.invoke()
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        self.assertEqual((fixture.root / 'src/app.py').read_text(), 'value = 2\n')
        run = fixture.root / 'adws/adw_data/sessions/guarded'
        planner = sorted(run.glob('planner/phases/*_plan_1/attempt-*/user.md'))
        self.assertTrue(planner)
        text = planner[-1].read_text()
        brief = json.JSONDecoder().raw_decode(text)[0]
        row = next((row for row in brief['context']['project_map'] if row['path'] == 'src/pkg00'), None)
        self.assertEqual(row, {'path': 'src/pkg00', 'area': 'src', 'file_count': 50})
        self.assertEqual(text.count('src/pkg00'), 1)
        self.assertNotIn('src/pkg00/module_0001.py', text)
        self.assertGreater(len(text.encode()), 131072)
        largest = max(len(f.read_bytes()) for f in run.rglob('attempt-*/user.md'))
        self.assertGreater(largest, 131072)   # would have been E2BIG as a single argv string


if __name__ == '__main__':
    unittest.main()
