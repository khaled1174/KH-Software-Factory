import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import sqlite3
import tempfile
import shutil
import subprocess
import sys
import unittest
from tests.contracts import COMMAND_COUNTS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("factory_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
spec2 = importlib.util.spec_from_file_location("command_snapshots", ROOT / ".claude/skills/kh/templates/adws/kh_commands.py")
commands = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(commands)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kh install test ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.package = self.base / "package source"
        self.target = self.base / "target project"
        self.target.mkdir()
        for rel, text in {
            "COMMANDS.md": "canonical command catalog",
            ".claude/commands/kh-help.md": "command v2",
            ".claude/skills/task-observer/SKILL.md": "observer",
            ".claude/skills/kh/references/model-choice.md": "model choice v2",
            ".claude/skills/kh-scope-guard/SKILL.md": "scope guard v2",
            "vendor/delegate-skills/README.md": "delegate",
            ".claude/skills/kh/scripts/install.py":
                "from pathlib import Path\nPath('adws').mkdir()\nPath('adws/runtime.py').write_text('runtime')\nPath('adws/kh_commands.py').write_text('helper')\nPath('.gitignore').write_text('.env\\n__pycache__/\\n')\n",
        }.items():
            path = self.package / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run_install(self, **kwargs):
        return installer.install(self.target, package=self.package, **kwargs)

    def hashes(self, root):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}

    def test_native_windows_entrypoints_explain_wsl2_before_platform_imports(self):
        launcher = '''
import argparse, ctypes, os, shutil, sys
from pathlib import Path
path = Path(sys.argv[1])
code = compile(path.read_bytes(), str(path), 'exec')
sys.path.insert(0, str(path.parent))
sys.modules['fcntl'] = None
os.name = 'nt'
sys.argv = [str(path), '--help']
exec(code, {'__name__': '__main__', '__file__': str(path)})
'''
        entries = [ROOT / 'install.py', ROOT / '.claude/skills/kh/scripts/install.py',
                   *sorted((ROOT / '.claude/skills/kh/templates/adws').glob('*.py'))]
        before = self.hashes(self.target)
        for path in entries:
            with self.subTest(entry=path.relative_to(ROOT).as_posix()):
                result = subprocess.run([sys.executable, '-B', '-c', launcher, str(path)],
                                        cwd=self.target, capture_output=True, text=True, timeout=10)
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertEqual('', result.stdout)
                self.assertEqual(1, len(result.stderr.splitlines()), result.stderr)
                for term in ('Windows', 'WSL2', 'WSL1', 'Ubuntu'):
                    self.assertIn(term, result.stderr)
        self.assertEqual(before, self.hashes(self.target))

    def test_fresh_paths_spaces_source_preserved_and_idempotent(self):
        before = self.hashes(self.package)
        report = self.run_install()
        self.assertTrue((self.target / 'adws/runtime.py').is_file())
        self.assertTrue((self.target / '.claude/skills/task-observer/SKILL.md').is_file())
        self.assertEqual(before, self.hashes(self.package))
        self.assertEqual([], self.run_install()['changes'])
        self.assertTrue((Path(report['backup']) / 'restore.json').is_file())

    def test_symlinked_system_tempdir_does_not_block_safe_staging(self):
        linked = self.base / 'temp-link'
        linked.symlink_to(self.base, target_is_directory=True)
        with patch.object(installer.tempfile, 'gettempdir', return_value=str(linked)):
            report = self.run_install()
        self.assertTrue(report['changes'])
        self.assertTrue((self.target / 'adws/runtime.py').is_file())

    def test_new_skill_conflict_blocks_whole_install_and_upgrade_is_reversible(self):
        target = self.target / '.claude/skills/kh-scope-guard/SKILL.md'
        target.parent.mkdir(parents=True)
        target.write_text('project-specific scope guard')
        before = self.hashes(self.target)
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual(before, self.hashes(self.target))
        report = self.run_install(upgrade=True)
        self.assertEqual('scope guard v2', target.read_text())
        self.assertEqual('model choice v2', (self.target / '.claude/skills/kh/references/model-choice.md').read_text())
        installer.restore(Path(report['backup']))
        self.assertEqual('project-specific scope guard', target.read_text())
        self.assertFalse((self.target / '.claude/skills/kh/references/model-choice.md').exists())

    def test_actual_package_installs_skills_with_metadata_and_license(self):
        report = installer.install(self.target, package=ROOT)
        for name in ['kh-scope-guard', 'fable-pack-audit', 'astra-pack-audit']:
            for file in ['SKILL.md', 'agents/openai.yaml']:
                rel = Path('.claude/skills') / name / file
                self.assertEqual((ROOT / rel).read_bytes(), (self.target / rel).read_bytes())
        model_guide = Path('.claude/skills/kh/references/model-choice.md')
        self.assertEqual((ROOT / model_guide).read_bytes(), (self.target / model_guide).read_bytes())
        # Audit skills retain their distinct command and metadata interfaces:
        # both adopted skills ship their attribution files byte-identical.
        for name in ['fable-pack-audit', 'astra-pack-audit']:
            rel = Path('.claude/skills') / name / 'ATTRIBUTION.md'
            self.assertEqual((ROOT / rel).read_bytes(), (self.target / rel).read_bytes())
        license_rel = Path('.claude/skills/kh-scope-guard/LICENSE')
        self.assertEqual((ROOT / license_rel).read_bytes(), (self.target / license_rel).read_bytes())
        self.assertFalse((self.target / '.claude/skills/kh-scope-guard/scripts/gate-check.mjs').exists())
        self.assertEqual(COMMAND_COUNTS['commands'], len(list((self.target / '.claude/commands').glob('kh-*.md'))))
        self.assertEqual([], installer.install(self.target, package=ROOT)['changes'])
        self.assertTrue(report['changes'])

        observer = self.target / '.claude/skills/task-observer'
        for rel in ('scripts/migrate-log.py', 'scripts/validate-skill-bundle.py',
                    'references/migration.md', 'references/skill-authoring.md', 'references/weekly-review.md'):
            self.assertFalse((observer / rel).exists(), rel)
        self.assertTrue((observer / 'scripts/observer_store.py').is_file())
        self.assertEqual((ROOT / '.claude/skills/task-observer/LICENSE.txt').read_bytes(),
                         (observer / 'LICENSE.txt').read_bytes())

    def test_redundant_router_and_cookbooks_are_not_installed(self):
        installer.install(self.target, package=ROOT)
        self.assertFalse((self.target / '.claude/skills/kh-model-router').exists())
        cookbooks = self.target / '.claude/skills/kh/cookbooks'
        for name in ('kh_overview', 'create_config', 'update_config', 'update_modules',
                     'update_adw', 'how_to_prompt_for_the_eng'):
            self.assertFalse((cookbooks / (name + '.md')).exists(), name)
        for name in ('install', 'run_adw'):
            self.assertTrue((cookbooks / (name + '.md')).is_file(), name)
        self.assertTrue((self.target / '.claude/skills/kh/references/model-choice.md').is_file())

    def test_actual_install_keeps_parallel_runtime_state_out_of_git(self):
        installer.install(self.target, package=ROOT)
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        paths = [
            ".kh/parallel/run/jobs/T1/stdout.log",
            ".kh/parallel/run/lanes/T1/.kh/project.db",
            ".kh/execution-decision.json",
            ".kh/execution-metadata.json",
        ]
        result = subprocess.run(
            ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--stdin"],
            cwd=self.target, input="\n".join(paths) + "\n", text=True,
            capture_output=True, check=True,
        )
        self.assertEqual(paths, result.stdout.splitlines())
        for module in ('engineering_guidance', 'planning_guidance', 'review_panel',
                       'execution_strategy', 'parallel_execution', 'parallel_candidate',
                       'candidate_integration', 'shared_context', 'native_replay',
                       'agent_antigravity', 'antigravity_policy', 'agent_gemini', 'agent_pi'):
            self.assertFalse((self.target / f'adws/adw_modules/{module}.py').exists())
            self.assertFalse((self.target / f'.claude/skills/kh/templates/adws/adw_modules/{module}.py').exists())
        attribution = self.target / '.claude/skills/kh/references/engineering-guidance'
        self.assertFalse((attribution / 'README.md').exists())
        self.assertTrue((attribution / 'LICENSE.mattpocock').is_file())

    def seed_retired_payloads(self):
        # Exact pre-retirement payloads: chains, Jev scout and role guidance.
        fixture = ROOT / 'tests/fixtures/retired'
        command = (fixture / 'kh-demo.md.txt').read_bytes()
        runtime = (fixture / 'adw_build.py.txt').read_bytes()
        scout = (fixture / 'jev_scout.py.txt').read_bytes()
        self.assertEqual('90e4bc4ea3991e7b1ac20bc8f73765df261b2fba8fceaa13f12fe25d6d50df37',
                         hashlib.sha256(command).hexdigest())
        self.assertEqual('0d17d735d0fa5e9e7d2a9270255824324fb3f436c0fc2f58fb3787d0691f555d',
                         hashlib.sha256(runtime).hexdigest())
        self.assertEqual('2cd774a3ff860f14c343ba152ef46abc703dd106d7439649ca95f6cfaa1f8a0a',
                         hashlib.sha256(scout).hexdigest())
        payloads = {'.claude/commands/kh-demo.md': command,
                    '.claude/skills/kh/templates/adws/adw_build.py': runtime,
                    'adws/adw_build.py': runtime,
                    '.claude/skills/kh/templates/adws/adw_modules/jev_scout.py': scout,
                    'adws/adw_modules/jev_scout.py': scout}
        for name, expected, paths in [
            ('engineering_guidance.py.txt', '60bffa1e80ff73810cd00827c4c792fd5ed86f7a7402199805c24e4272c2d3ff',
             ['.claude/skills/kh/templates/adws/adw_modules/engineering_guidance.py',
              'adws/adw_modules/engineering_guidance.py']),
            ('planning_guidance.py.txt', '7b1c5e86eb1a73e58d439f048975063391f560f7843a2fb2c250c5810db404cb',
             ['.claude/skills/kh/templates/adws/adw_modules/planning_guidance.py',
              'adws/adw_modules/planning_guidance.py']),
            ('engineering-guidance-README.md.txt', '3da39d111b95cd5e077d68f1564b5bb4eef11dbae5bfa632823df8c373564f09',
             ['.claude/skills/kh/references/engineering-guidance/README.md']),
        ]:
            data = (fixture / name).read_bytes()
            self.assertEqual(expected, hashlib.sha256(data).hexdigest())
            payloads.update(dict.fromkeys(paths, data))
        shared = (fixture / 'shared_context.py.txt').read_bytes()
        payloads.update(dict.fromkeys(('.claude/skills/kh/templates/adws/adw_modules/shared_context.py',
                                      'adws/adw_modules/shared_context.py'), shared))
        retired_reference = (fixture / 'antigravity.md.txt').read_bytes()
        payloads['.claude/skills/kh/references/antigravity.md'] = retired_reference
        for rel, data in payloads.items():
            path = self.target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o640)
        return payloads

    def test_real_retired_payloads_are_removed_and_restore_recovers_both_layouts(self):
        payloads = self.seed_retired_payloads()
        for rel, data in payloads.items():
            self.assertIn(hashlib.sha256(data).hexdigest(), installer.RETIRED_PATHS.get(rel, set()))
        custom = self.target / '.claude/commands/kh-owner.md'
        custom.write_text('owner command')
        report = installer.install(self.target, package=ROOT, upgrade=True)
        self.assertEqual(set(payloads), {e['path'] for e in report['changes'] if e['action'] == 'remove'})
        for rel in payloads:
            self.assertFalse((self.target / rel).exists())
            self.assertEqual(payloads[rel], (Path(report['backup']) / 'originals' / rel).read_bytes())
        installer.restore(Path(report['backup']))
        for rel, data in payloads.items():
            self.assertEqual(data, (self.target / rel).read_bytes())
            self.assertEqual(0o640, (self.target / rel).stat().st_mode & 0o777)
        self.assertEqual('owner command', custom.read_text())

    def test_real_modified_or_unknown_retired_payloads_are_preserved(self):
        self.seed_retired_payloads()
        changed = {'.claude/commands/kh-demo.md': b'owner command edits',
                   'adws/adw_build.py': b'unknown older runtime version',
                   'adws/adw_modules/jev_scout.py': b'owner scout edits',
                   'adws/adw_modules/engineering_guidance.py': b'owner guidance edits',
                   '.claude/skills/kh/references/engineering-guidance/README.md': b'owner reference edits'}
        for rel, data in changed.items():
            (self.target / rel).write_bytes(data)
        report = installer.install(self.target, package=ROOT, upgrade=True)
        for rel, data in changed.items():
            self.assertEqual(data, (self.target / rel).read_bytes())
        self.assertEqual(set(changed), {e['path'] for e in report.get('kept_retired_files', [])})
        self.assertFalse((self.target / '.claude/skills/kh/templates/adws/adw_build.py').exists())
        self.assertFalse((self.target / '.claude/skills/kh/templates/adws/adw_modules/jev_scout.py').exists())

    def test_installed_markdown_references_resolve(self):
        installer.install(self.target, package=ROOT)
        # These exact occurrences name generated outputs, not installed inputs.
        output_prompts = {'project_architect', 'documenter', 'planner'}
        pattern = re.compile(r'(?<![\w/])(?:\.claude/|skills/kh/|(?:\.\./)*(?:skills/kh/)?references/|docs/)[\w./-]*\.md')

        def missing():
            failures = []
            paths = list((self.target / '.claude/commands').glob('*.md'))
            paths += list((self.target / '.claude/skills').rglob('*.md'))
            for path in paths:
                text = path.read_text()
                for match in pattern.finditer(text):
                    ref = match.group()
                    if (ref == 'docs/agent-guide/conditional_docs.md'
                            and path.name == 'system.md'
                            and path.parent.name in output_prompts):
                        continue
                    if (ref == 'docs/agent-guide/commands.md' and path.name == 'system.md'
                            and path.parent.name == 'project_architect'):
                        continue  # Controller-generated output, never an installed input.
                    if (path == self.target / '.claude/skills/kh/references/project-context.md'
                            and ref in {'docs/agent-guide/project-map.md', 'docs/agent-guide/architecture.md',
                                        'docs/agent-guide/rules.md', 'docs/agent-guide/testing.md',
                                        'docs/agent-guide/commands.md', 'docs/agent-guide/user-journeys.md',
                                        'docs/agent-guide/conditional_docs.md', 'docs/agent-guide/csv-export.md'}):
                        continue  # Exact generated-output examples in the installed schema reference.
                    suffix = text[match.end():match.end() + 55]
                    distribution = re.match(r'`? in the\s+(?:KH distribution|distribution|KH Software Factory package)', suffix)
                    bases = [ROOT] if distribution else [self.target, path.parent]
                    if not distribution and path.is_relative_to(self.target / '.claude/skills/kh'):
                        bases.append(self.target / '.claude/skills/kh')
                    if not any((base / ref).is_file() for base in bases):
                        failures.append(f'{path.relative_to(self.target)}:{text[:match.start()].count(chr(10)) + 1}: {ref}')
                for ref in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
                    if re.match(r'(?:[a-z]+:|#)', ref):
                        continue
                    local = ref.split('#', 1)[0]
                    if local and not (path.parent / local).exists():
                        failures.append(f'{path.relative_to(self.target)}: broken Markdown link {ref}')
            return failures

        self.assertEqual([], missing())
        # A distribution file cannot silently satisfy a promised installed input.
        command = self.target / '.claude/commands/kh-help.md'
        with command.open('a') as stream:
            stream.write('\nRead docs/PROJECT_CONTEXT.md.\n')
        self.assertEqual(1, len(missing()))
        with command.open('a') as stream:
            stream.write('See docs/does-not-exist.md in the distribution.\n')
        self.assertEqual(2, len(missing()))
        with command.open('a') as stream:
            stream.write('Read [missing](../../../missing.md).\n')
        self.assertEqual(3, len(missing()))

    def test_runtime_instructions_match_current_controller_contract(self):
        installer.install(self.target, package=ROOT)
        for name in ['kh-start', 'kh-simple-sdlc']:
            text = (self.target / f'.claude/commands/{name}.md').read_text()
            with self.subTest(command=name):
                self.assertIn('execution', text)
                self.assertIn('--dry-run', text)
                self.assertNotRegex(text, r'eighteen total|ten total|900-second|10,800|1,800')
        start = (self.target / '.claude/commands/kh-start.md').read_text()
        self.assertNotIn('/kh-bootstrap --refresh', start)
        self.assertIn('--refresh-context', start)
        setup = (self.target / '.claude/commands/kh-setup-models.md').read_text()
        self.assertIn('builder, reviewer and documenter', setup)
        cookbook = (self.target / '.claude/skills/kh/cookbooks/run_adw.md').read_text()
        self.assertIn('run-continuous', cookbook)
        self.assertNotIn('run-approved', cookbook)
        self.assertNotIn('default global maximum of 10', cookbook)
        for source in [ROOT / 'COMMANDS.md', self.target / '.claude/skills/kh/references/commands.md']:
            for verb in ['propose-execution', 'approve-execution', 'run-approved',
                         'parallel-status', '--resume-run', 'parallel-resolve']:
                with self.subTest(source=source.name, verb=verb):
                    self.assertNotIn(verb, source.read_text())
        reference = self.target / '.claude/skills/kh/references/execution-decision.md'
        self.assertFalse(reference.exists())
        policy = (ROOT / 'docs/ARCHITECTURE.md').read_text()
        self.assertIn('Sequential execution', policy)
        self.assertIn('LEGACY_PARALLEL_STATE', policy)

    def test_help_uses_one_catalog_delivered_from_the_distribution(self):
        installer.install(self.target, package=ROOT)
        relative = '.claude/skills/kh/references/commands.md'
        catalog = self.target / relative
        self.assertTrue(catalog.is_file(), 'project installation must deliver the canonical catalog')
        self.assertEqual((ROOT / 'COMMANDS.md').read_bytes(), catalog.read_bytes())
        for flag in ('--agent NAME', '--db PATH', '--advisory', '--evidence PATH'):
            self.assertIn(flag, catalog.read_text())
        command = self.target / '.claude/commands/kh-help.md'
        self.assertIn(relative, command.read_text())
        self.assertNotIn('| /kh-', command.read_text(), 'help must read the catalog rather than duplicate it')
        user_target = self.base / 'user-install'
        user_target.mkdir()
        installer.install(user_target, package=ROOT, user=True)
        self.assertTrue((user_target / '.claude/commands/kh-help.md').is_file())
        self.assertFalse((user_target / relative).exists(), 'global installation stays commands-only')

    def test_dry_run_no_target_write(self):
        report = self.run_install(dry_run=True)
        self.assertTrue(report['changes'])
        self.assertEqual([], list(self.target.iterdir()))

    def test_installed_model_switch_and_timing_state_are_ignored(self):
        installer.install(self.target, package=ROOT)
        subprocess.run(['git', 'init', '-q', str(self.target)], check=True)
        code = '''
import sys
from pathlib import Path
sys.path.insert(0, 'adws')
from adw_modules import timing
root = Path.cwd()
old_state = root / '.kh/model-switch/ignore-regression/state.json'
old_state.parent.mkdir(parents=True)
old_state.write_text('{}')
timing.record(root, 'build', 1.0)
'''
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=self.target,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = ['.kh/model-switch/ignore-regression/state.json', '.kh/timing-history.json']
        self.assertTrue(all((self.target / path).is_file() for path in paths))
        ignored = subprocess.run(['git', '-c', 'core.excludesFile=/dev/null', 'check-ignore', '--stdin'],
                                 cwd=self.target, input='\n'.join(paths) + '\n',
                                 capture_output=True, text=True)
        self.assertEqual(ignored.stdout.splitlines(), paths, ignored.stderr)
        status = subprocess.run(['git', '-c', 'core.excludesFile=/dev/null', 'status', '--porcelain',
                                 '--', *paths], cwd=self.target, capture_output=True, text=True, check=True)
        self.assertEqual(status.stdout, '')

    def test_conflict_stops_entire_install(self):
        (self.target / 'adws').mkdir()
        (self.target / 'adws/runtime.py').write_text('user version')
        before = self.hashes(self.target)
        self.assertTrue(self.run_install(dry_run=True)['upgrade_required'])
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual(before, self.hashes(self.target))

    def test_upgrade_backup_restore_preserves_user_content_and_mode(self):
        target = self.target / 'adws/runtime.py'
        target.parent.mkdir()
        target.write_text('user version')
        target.chmod(0o600)
        (self.target / '.gitignore').write_text('local-rule\n')
        report = self.run_install(upgrade=True)
        backup = Path(report['backup'])
        self.assertEqual('user version', (backup/'originals/adws/runtime.py').read_text())
        self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertIn('local-rule', (self.target / '.gitignore').read_text())
        installer.restore(backup, dry_run=True)
        self.assertEqual('runtime', target.read_text())
        installer.restore(backup)
        self.assertEqual('user version', target.read_text())
        self.assertEqual('local-rule\n', (self.target / '.gitignore').read_text())
        self.assertFalse((self.target/'.claude/commands/kh-help.md').exists())

    def test_restore_refuses_later_edits_before_any_write(self):
        report = self.run_install()
        (self.target/'adws/runtime.py').write_text('later work')
        before = self.hashes(self.target)
        with self.assertRaises(ValueError):
            installer.restore(Path(report['backup']))
        self.assertEqual(before, self.hashes(self.target))

    def test_write_failure_rolls_back_already_written_files(self):
        target = self.target/'.claude/commands/kh-help.md'
        target.parent.mkdir(parents=True)
        target.write_text('old command')
        original = installer.atomic_write
        calls = 0
        def fail_second(path, data, mode):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated disk failure')
            return original(path, data, mode)
        with patch.object(installer, 'atomic_write', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.run_install(upgrade=True)
        self.assertEqual('old command', target.read_text())
        self.assertFalse((self.target/'adws/runtime.py').exists())

    def test_parent_symlink_refused(self):
        external = self.base/'external'
        external.mkdir()
        (self.target/'.claude').symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual([], list(external.iterdir()))

    def test_destination_symlink_refused(self):
        external = self.base/'keep.txt'
        external.write_text('keep')
        (self.target/'adws').mkdir()
        (self.target/'adws/runtime.py').symlink_to(external)
        with self.assertRaises(ValueError):
            self.run_install(upgrade=True)
        self.assertEqual('keep', external.read_text())

    def test_source_symlink_refused(self):
        (self.package/'.claude/commands/link.md').symlink_to(self.package/'.claude/commands/kh-help.md')
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual([], list(self.target.iterdir()))

    def test_user_scope_only_commands(self):
        self.run_install(user=True)
        self.assertTrue((self.target/'.claude/commands/kh-help.md').exists())
        self.assertFalse((self.target/'.claude/skills').exists())
        self.assertFalse((self.target/'adws').exists())
        self.assertFalse((self.target/'vendor').exists())


class RetiredInstallTests(unittest.TestCase):
    run_install = InstallTests.run_install
    hashes = InstallTests.hashes

    def setUp(self):
        InstallTests.setUp(self)
        self.retired = {}
        registry = patch.object(installer, 'RETIRED_PATHS', self.retired, create=True)
        registry.start()
        self.addCleanup(registry.stop)

    def test_retired_visualizer_files_require_explicit_component_upgrade(self):
        ui = Path('.claude/skills/kh/apps/visualizer')
        package_json = self.package / ui / 'package.json'
        package_json.parent.mkdir(parents=True)
        package_json.write_text('{}')
        board = self.old_file((ui / 'src/components/ExecutionBoard.vue').as_posix(), b'old board\n')
        report = self.run_install(upgrade=True)
        self.assertTrue(board.is_file(), 'a core-only upgrade must preserve the installed UI')
        self.assertFalse(any(item['action'] == 'remove' for item in report['changes']))
        report = self.run_install(upgrade=True, with_visualizer=True)
        self.assertFalse(board.exists())
        installer.restore(Path(report['backup']))
        self.assertEqual(board.read_bytes(), b'old board\n')
        self.assertEqual(board.stat().st_mode & 0o777, 0o640)

    def old_file(self, rel='.claude/commands/kh-retired.md', data=b'old command\n'):
        path = self.target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o640)
        self.retired[rel] = {hashlib.sha256(data).hexdigest()}
        return path

    def payload(self):
        return {rel: value for rel, value in self.hashes(self.target).items()
                if not rel.startswith('.kh-backups/')}

    def test_retirement_only_upgrade_backup_and_restore(self):
        self.run_install()
        target = self.old_file()
        rel = target.relative_to(self.target).as_posix()
        report = self.run_install(upgrade=True)
        self.assertFalse(target.exists())
        self.assertEqual([rel], [entry['path'] for entry in report['changes']])
        entry = report['changes'][0]
        self.assertEqual('remove', entry['action'])
        self.assertEqual(f'removed (retired): {rel}', entry['message'])
        self.assertIsNone(entry['new_sha256'])
        backup = Path(report['backup'])
        self.assertEqual(b'old command\n', (backup / 'originals' / rel).read_bytes())
        saved = json.loads((backup / 'restore.json').read_text())['files'][0]
        self.assertIsNone(saved['installed_sha256'])
        self.assertEqual(0o640, saved['original_mode'])
        self.assertEqual([], self.run_install(upgrade=True)['changes'])
        installer.restore(backup, dry_run=True)
        self.assertFalse(target.exists())
        installer.restore(backup)
        self.assertEqual(b'old command\n', target.read_bytes())
        self.assertEqual(0o640, target.stat().st_mode & 0o777)

    def test_modified_unknown_and_unregistered_files_stay(self):
        modified = self.old_file()
        modified.write_bytes(b'owner changes\n')
        unknown = self.old_file('.claude/commands/kh-other-retired.md')
        unknown.write_bytes(b'unrecognized older release\n')
        custom = self.target / '.claude/commands/kh-owner.md'
        custom.write_bytes(b'owner command\n')
        report = self.run_install(upgrade=True)
        self.assertEqual({modified.relative_to(self.target).as_posix(),
                          unknown.relative_to(self.target).as_posix()},
                         {entry['path'] for entry in report.get('kept_retired_files', [])})
        self.assertEqual(b'owner changes\n', modified.read_bytes())
        self.assertEqual(b'unrecognized older release\n', unknown.read_bytes())
        self.assertEqual(b'owner command\n', custom.read_bytes())
        self.assertFalse(any(entry['action'] == 'remove' for entry in report['changes']))

    def test_plain_installs_and_upgrade_preview_do_not_retire(self):
        target = self.old_file()
        for kwargs in ({}, {'user': True}, {'upgrade': True, 'dry_run': True}):
            with self.subTest(options=kwargs):
                before = self.hashes(self.target)
                report = self.run_install(**kwargs)
                self.assertEqual(b'old command\n', target.read_bytes())
                if kwargs.get('dry_run'):
                    self.assertEqual(before, self.hashes(self.target))
                    self.assertTrue(any(e['action'] == 'remove' for e in report['changes']))
                else:
                    self.assertFalse(any(e['action'] == 'remove' for e in report['changes']))

    def test_user_upgrade_retires_commands_only(self):
        command = self.old_file()
        runtime = self.old_file('adws/retired.py', b'old runtime\n')
        template = self.old_file('.claude/skills/kh/templates/adws/retired.py', b'old template\n')
        report = self.run_install(user=True, upgrade=True)
        self.assertFalse(command.exists())
        self.assertEqual(b'old runtime\n', runtime.read_bytes())
        self.assertEqual(b'old template\n', template.read_bytes())
        self.assertEqual(['.claude/commands/kh-retired.md'],
                         [e['path'] for e in report['changes'] if e['action'] == 'remove'])

    def test_retirement_refuses_symlinks_and_directories_before_any_write(self):
        target = self.old_file()
        target.unlink()
        outside = self.base / 'owner-file'
        outside.write_bytes(b'owner data')
        for kind in ('symlink', 'directory'):
            with self.subTest(kind=kind):
                target.symlink_to(outside) if kind == 'symlink' else target.mkdir()
                before = self.hashes(self.target)
                try:
                    with self.assertRaises(ValueError):
                        self.run_install(upgrade=True)
                    self.assertEqual(before, self.hashes(self.target))
                    self.assertEqual(b'owner data', outside.read_bytes())
                finally:
                    target.unlink() if kind == 'symlink' else target.rmdir()

    def test_unsafe_retirement_registry_path_is_rejected_before_writes(self):
        outside = self.base / 'owner-file'
        outside.write_bytes(b'owner data')
        self.retired['../owner-file'] = {hashlib.sha256(b'owner data').hexdigest()}
        with self.assertRaisesRegex(ValueError, 'Unsafe.*retired'):
            self.run_install(upgrade=True)
        self.assertEqual([], list(self.target.iterdir()))
        self.assertEqual(b'owner data', outside.read_bytes())

    def test_still_shipped_and_kept_owner_destinations_never_retire(self):
        command = self.old_file('.claude/commands/kh-help.md')
        config = self.old_file('adws/adw_kh_config/kh.config.yaml', b'owner config\n')
        with patch.object(installer, 'desired_files', return_value={
                '.claude/commands/kh-help.md': (b'command v2', 0o644),
                'adws/adw_kh_config/kh.config.yaml': (b'template config\n', 0o644)}):
            report = self.run_install(upgrade=True)
        self.assertEqual(b'command v2', command.read_bytes())
        self.assertEqual(b'owner config\n', config.read_bytes())
        self.assertFalse(any(e['action'] == 'remove' for e in report['changes']))
        self.assertIn('adws/adw_kh_config/kh.config.yaml',
                      [e['path'] for e in report['kept_project_files']])

    def test_recreated_retired_targets_block_entire_restore(self):
        target = self.old_file()
        report = self.run_install(upgrade=True)
        self.assertFalse(target.exists())
        backup = Path(report['backup'])
        plan = backup / 'restore.json'
        manifest = json.loads(plan.read_text())
        retired = [e for e in manifest['files'] if e['installed_sha256'] is None]
        manifest['files'] = retired + [e for e in manifest['files'] if e['installed_sha256'] is not None]
        plan.write_text(json.dumps(manifest))
        for kind in ('file', 'directory', 'dangling symlink'):
            with self.subTest(kind=kind):
                if kind == 'file':
                    target.write_text('later owner work')
                elif kind == 'directory':
                    target.mkdir()
                else:
                    target.symlink_to(self.base / 'absent-owner-target')
                with patch.object(installer, 'atomic_write', wraps=installer.atomic_write) as write, \
                     patch.object(Path, 'unlink', autospec=True, side_effect=Path.unlink) as remove:
                    with self.assertRaises(ValueError):
                        installer.restore(backup)
                    write.assert_not_called()
                    remove.assert_not_called()
                self.assertEqual('runtime', (self.target / 'adws/runtime.py').read_text())
                target.rmdir() if kind == 'directory' else target.unlink()

    def test_retired_backup_checksum_is_checked_before_any_restore(self):
        target = self.old_file()
        report = self.run_install(upgrade=True)
        self.assertFalse(target.exists())
        backup = Path(report['backup'])
        (backup / 'originals/.claude/commands/kh-retired.md').write_bytes(b'corrupt')
        before = self.payload()
        with self.assertRaisesRegex(ValueError, 'Backup checksum mismatch'):
            installer.restore(backup)
        self.assertEqual(before, self.payload())

    def test_install_failure_restores_removed_files_and_prior_writes(self):
        first = self.old_file('.claude/commands/kh-retired-a.md')
        second = self.old_file('.claude/commands/kh-retired-b.md')
        before = self.payload()
        unlink = Path.unlink
        def fail_second(path, *args, **kwargs):
            if path == second:
                raise OSError('retirement failure')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=fail_second):
            with self.assertRaisesRegex(OSError, 'retirement failure'):
                self.run_install(upgrade=True)
        self.assertEqual(before, self.payload())
        self.assertEqual(0o640, first.stat().st_mode & 0o777)

    def test_restore_failure_returns_restored_retirement_to_absence(self):
        target = self.old_file()
        report = self.run_install(upgrade=True)
        self.assertFalse(target.exists())
        before = self.payload()
        unlink = Path.unlink
        failed = False
        def fail_later(path, *args, **kwargs):
            nonlocal failed
            if path == self.target / 'adws/kh_commands.py' and not failed:
                self.assertTrue(target.is_file())
                self.assertFalse((self.target / 'adws/runtime.py').exists())
                failed = True
                raise OSError('restore failure')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=fail_later):
            with self.assertRaisesRegex(OSError, 'restore failure'):
                installer.restore(Path(report['backup']))
        self.assertTrue(failed)
        self.assertEqual(before, self.payload())
        self.assertFalse(target.exists())


class CommandCatalogTests(unittest.TestCase):
    def test_user_commands_resolve_contract_from_separate_target_project(self):
        with tempfile.TemporaryDirectory(prefix='kh global contracts ') as temporary:
            base = Path(temporary).resolve()
            user, project = base / 'user home', base / 'target project'
            user.mkdir()
            project.mkdir()
            installer.install(user, package=ROOT, user=True)
            installer.install(project, package=ROOT)
            self.assertFalse((user / '.claude/skills').exists())
            self.assertFalse((user / 'adws').exists())
            commands = [p for p in (user / '.claude/commands').glob('kh-*.md')
                        if p.stem not in {'kh-project', 'kh-help'}]
            self.assertEqual(len(commands), 16)
            for command in commands:
                with self.subTest(command=command.name):
                    text = command.read_text()
                    self.assertIn('Git root', text)
                    self.assertIn('ancestor containing `adws/`', text)
                    self.assertIn('From that project root, read\n', text)
                    match = re.search(r'From that project root, read\n`([^`]+)`', text)
                    self.assertIsNotNone(match)
                    relative = Path(match.group(1))
                    self.assertEqual(relative, Path('.claude/skills/kh/references/execution-contract.md'))
                    self.assertFalse((user / relative).exists())
                    self.assertTrue((project / relative).is_file())
                    self.assertIn('also applies to commands installed in the user scope', text)

    def test_sixteen_installed_commands_resolve_their_shared_contract(self):
        guarded = {'kh-bootstrap', 'kh-models', 'kh-plan-review', 'kh-scope',
                   'kh-setup-models', 'kh-start'}
        with tempfile.TemporaryDirectory(prefix='kh contract links ') as temporary:
            target = Path(temporary).resolve()
            installer.install(target, package=ROOT)
            reference = target / '.claude/skills/kh/references/execution-contract.md'
            self.assertTrue(reference.is_file())
            self.assertEqual(reference.read_bytes(),
                (ROOT / '.claude/skills/kh/references/execution-contract.md').read_bytes())
            commands = [p for p in (target / '.claude/commands').glob('kh-*.md')
                        if p.stem not in {'kh-project', 'kh-help'}]
            self.assertEqual(len(commands), 16)
            for command in commands:
                with self.subTest(command=command.name):
                    section = 'guarded-commands' if command.stem in guarded else 'standard-commands'
                    link = '../skills/kh/references/execution-contract.md#' + section
                    self.assertEqual(command.read_text().count('](' + link + ')'), 1)
                    self.assertIn('## ' + section.replace('-', ' ').capitalize(), reference.read_text())
                    self.assertEqual((command.parent / link.split('#')[0]).resolve(), reference)

    def test_eighteen_commands_and_twelve_mapped_scripts(self):
        files = sorted((ROOT / '.claude/commands').glob('kh-*.md'))
        self.assertEqual(COMMAND_COUNTS['commands'], len(files))
        mappings = []
        for path in files:
            text = path.read_text()
            self.assertIn('$ARGUMENTS', text)
            match = re.search(r'Mapped script: `(adws/[^`]+)`', text)
            if match:
                mappings.append(match.group(1))
                self.assertTrue((ROOT / '.claude/skills/kh/templates' / match.group(1)).is_file(), match.group(1))
        self.assertEqual(COMMAND_COUNTS['mapped_scripts'], len(set(mappings)))


class SnapshotTests(unittest.TestCase):
    def test_missing_database_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'missing.db'
            with self.assertRaises(ValueError):
                commands.snapshot(db, 'sessions')
            self.assertFalse(db.exists())

    def test_injection_is_literal_and_read_only(self):
        with tempfile.TemporaryDirectory(prefix='kh db ') as tmp:
            db = Path(tmp)/'trace.db'
            with sqlite3.connect(db) as conn:
                conn.executescript('CREATE TABLE sessions(adw_id TEXT,status TEXT,request TEXT,total_tokens INTEGER,total_cost REAL,started_at TEXT); CREATE TABLE phases(adw_id TEXT,seq INTEGER,name TEXT,kind TEXT,owner TEXT,status TEXT,attempt INTEGER);')
                conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)', ('run1','ok','task',1,0.1,'2026'))
                conn.execute('INSERT INTO phases VALUES(?,?,?,?,?,?,?)', ('run1',1,'build','agent','builder','ok',1))
            before = db.read_bytes()
            self.assertEqual([], commands.snapshot(db,'phases',"' OR 1=1 --")['rows'])
            self.assertEqual(1, len(commands.snapshot(db,'phases')['rows']))
            self.assertEqual(before, db.read_bytes())




class ArchiveRoundTripTests(unittest.TestCase):
    """The shipped ZIP, not the working tree, must install. ZIP drops empty
    directories needed by runtime staging."""

    def test_package_rebuilt_through_zip_still_stages_runtime(self):
        import zipfile
        with tempfile.TemporaryDirectory(prefix="kh-zip-") as temporary:
            archive = Path(temporary) / "package.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(ROOT.rglob("*")):
                    rel = path.relative_to(ROOT)
                    if path.is_file() and not any(part in {".git", "__pycache__", "node_modules"} for part in rel.parts):
                        bundle.write(path, str(rel))
            extracted = Path(temporary) / "extracted"
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(extracted)
            stage = Path(temporary) / "stage"
            stage.mkdir()
            result = subprocess.run([sys.executable, str(extracted / ".claude/skills/kh/scripts/install.py")],
                                    cwd=stage, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((stage / "adws/adw_data/harness_engineering").is_dir())
            self.assertTrue((stage / "adws/adw_modules/scope.py").is_file())

    def test_missing_optional_template_folder_is_tolerated(self):
        with tempfile.TemporaryDirectory(prefix="kh-tpl-") as temporary:
            package = Path(temporary) / "pkg"
            shutil.copytree(ROOT / ".claude/skills/kh", package / ".claude/skills/kh",
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
            shutil.rmtree(package / ".claude/skills/kh/templates/harness_engineering")
            stage = Path(temporary) / "stage"
            stage.mkdir()
            result = subprocess.run([sys.executable, str(package / ".claude/skills/kh/scripts/install.py")],
                                    cwd=stage, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((stage / "adws/adw_data/harness_engineering").is_dir())


CONFIG = "adws/adw_kh_config/kh.config.yaml"
PROJECT_CONFIG = "project: protected paths, allowlist, timeouts\n"


class ProjectConfigKeptTests(unittest.TestCase):
    """An upgrade must not replace the project's kh.config.yaml.

    The file holds the owner's protected paths, env allowlist, quality checks,
    timeouts and budgets. Replacing it with the template on every upgrade
    removed those safeguards until someone re-applied them from the backup.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kh cfg test ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.package = self.base / "package"
        self.target = self.base / "project"
        self.target.mkdir()
        self.write_package(config="template v2\n", runtime="runtime v2")

    def write_package(self, *, config, runtime):
        script = (
            "from pathlib import Path\n"
            "Path('adws/adw_kh_config').mkdir(parents=True)\n"
            f"Path('{CONFIG}').write_text({config!r})\n"
            f"Path('adws/runtime.py').write_text({runtime!r})\n"
            "Path('adws/kh_commands.py').write_text('helper')\n"
            "Path('.gitignore').write_text('.env\\n')\n")
        for rel, text in {
            "COMMANDS.md": "canonical command catalog",
            ".claude/commands/kh-help.md": "command",
            ".claude/skills/kh/references/model-choice.md": "model choice",
            "vendor/delegate-skills/README.md": "delegate",
            ".claude/skills/kh/scripts/install.py": script,
        }.items():
            path = self.package / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def install(self, **kwargs):
        return installer.install(self.target, package=self.package, **kwargs)

    def project_config(self, text=PROJECT_CONFIG):
        path = self.target / CONFIG
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return path

    def test_upgrade_keeps_project_config_and_saves_the_package_version(self):
        config = self.project_config()
        runtime = self.target / "adws/runtime.py"
        runtime.write_text("runtime v1")
        report = self.install(upgrade=True)
        self.assertEqual(PROJECT_CONFIG, config.read_text())
        self.assertEqual("runtime v2", runtime.read_text())
        kept = report["kept_project_files"]
        self.assertEqual([CONFIG], [entry["path"] for entry in kept])
        saved = Path(kept[0]["package_version_saved_as"])
        self.assertEqual("template v2\n", saved.read_text())
        self.assertEqual(self.target / ".kh-backups", saved.parent)
        self.assertIn("diff", kept[0]["compare"])
        self.assertNotIn(CONFIG, [item["path"] for item in report["changes"]])
        self.assertNotIn(CONFIG, report["conflicts"])

    def test_differing_project_config_alone_does_not_block_a_plain_install(self):
        config = self.project_config()
        report = self.install()
        self.assertEqual(PROJECT_CONFIG, config.read_text())
        self.assertEqual([], report["conflicts"])
        self.assertTrue((self.target / "adws/runtime.py").is_file())

    def test_dry_run_reports_it_and_writes_nothing(self):
        self.project_config()
        report = self.install(upgrade=True, dry_run=True)
        self.assertEqual([CONFIG], [e["path"] for e in report["kept_project_files"]])
        self.assertNotIn("package_version_saved_as", report["kept_project_files"][0])
        self.assertFalse((self.target / ".kh-backups").exists())
        self.assertFalse((self.target / "adws/runtime.py").exists())

    def test_saved_package_version_is_stable_across_repeat_upgrades(self):
        self.project_config()
        first = self.install(upgrade=True)["kept_project_files"][0]["package_version_saved_as"]
        second = self.install(upgrade=True)["kept_project_files"][0]["package_version_saved_as"]
        self.assertEqual(first, second)
        saved = [p.name for p in (self.target / ".kh-backups").iterdir() if p.is_file()]
        self.assertEqual(1, len(saved))

    def test_a_newer_package_config_is_saved_under_its_own_name(self):
        self.project_config()
        old = self.install(upgrade=True)["kept_project_files"][0]["package_version_saved_as"]
        self.write_package(config="template v3\n", runtime="runtime v3")
        new = self.install(upgrade=True)["kept_project_files"][0]["package_version_saved_as"]
        self.assertNotEqual(old, new)
        self.assertEqual("template v2\n", Path(old).read_text())
        self.assertEqual("template v3\n", Path(new).read_text())

    def test_fresh_install_still_creates_the_template_config(self):
        report = self.install()
        self.assertEqual("template v2\n", (self.target / CONFIG).read_text())
        self.assertNotIn("kept_project_files", report)
        self.assertEqual([], self.install()["changes"])

    def test_identical_project_config_is_not_reported_as_kept(self):
        self.project_config("template v2\n")
        report = self.install()
        self.assertNotIn("kept_project_files", report)

    def test_reset_config_is_an_explicit_upgrade_that_backs_up_the_old_file(self):
        config = self.project_config()
        with self.assertRaises(ValueError):
            self.install(reset_config=True)
        self.assertEqual(PROJECT_CONFIG, config.read_text())
        report = self.install(upgrade=True, reset_config=True)
        self.assertEqual("template v2\n", config.read_text())
        self.assertNotIn("kept_project_files", report)
        original = Path(report["backup"]) / "originals" / CONFIG
        self.assertEqual(PROJECT_CONFIG, original.read_text())

    def test_restore_does_not_touch_the_kept_config(self):
        config = self.project_config()
        (self.target / "adws").mkdir(exist_ok=True)
        (self.target / "adws/runtime.py").write_text("runtime v1")
        report = self.install(upgrade=True)
        installer.restore(Path(report["backup"]))
        self.assertEqual(PROJECT_CONFIG, config.read_text())
        self.assertEqual("runtime v1", (self.target / "adws/runtime.py").read_text())

    def test_command_line_flag_requires_a_project(self):
        result = subprocess.run([sys.executable, str(ROOT / "install.py"), "--user", "--reset-config"],
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(0, result.returncode)


class ProjectPromptsKeptTests(unittest.TestCase):
    """Project instructions survive installation and remain explicit owner prose."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kh prompt test ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.package = self.base / "package"
        self.target = self.base / "project"
        self.target.mkdir()
        self.prompts = {
            f"adws/adw_data/prompt_engineering/{role}/{name}.md": "package prompt v2\n"
            for role in ("planner", "plan_reviewer", "builder", "reviewer")
            for name in ("system", "user")
        }
        self.prompts["adws/adw_data/harness_engineering/custom-system.md"] = "package custom prompt\n"
        self.prompts["adws/adw_data/harness_engineering/custom-user.md"] = "package custom user prompt\n"
        payload = {CONFIG: "template configuration\n", 'adws/kh_commands.py': 'helper', **self.prompts}
        script = ("from pathlib import Path\n"
                  f"for rel, text in {payload!r}.items():\n"
                  "    path = Path(rel)\n"
                  "    path.parent.mkdir(parents=True, exist_ok=True)\n"
                  "    path.write_text(text)\n")
        for rel, text in {
            "COMMANDS.md": "canonical command catalog",
            ".claude/commands/kh-help.md": "command",
            ".claude/skills/kh/SKILL.md": "controller",
            "vendor/delegate-skills/README.md": "delegate",
            ".claude/skills/kh/scripts/install.py": script,
        }.items():
            path = self.package / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def install(self, **kwargs):
        return installer.install(self.target, package=self.package, **kwargs)

    def custom_prompts(self):
        custom = {}
        for rel in self.prompts:
            if "prompt_engineering/" not in rel:
                continue
            path = self.target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            text = f"F2B instructions for {rel}\n"
            path.write_text(text)
            path.chmod(0o600)
            custom[rel] = text
        return custom

    def test_upgrade_keeps_four_role_system_and_user_prompts_with_distinct_comparison_paths(self):
        custom = self.custom_prompts()
        report = self.install(upgrade=True)
        kept = {entry["path"]: entry for entry in report["kept_project_files"]}
        self.assertEqual(set(custom), set(kept))
        saved_paths = set()
        for rel, text in custom.items():
            path = self.target / rel
            self.assertEqual(text, path.read_text())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            saved = Path(kept[rel]["package_version_saved_as"])
            saved_paths.add(saved)
            self.assertEqual(self.prompts[rel], saved.read_text())
            self.assertEqual(Path(rel).parent, saved.parent.relative_to(self.target / ".kh-backups/package-files"))
            self.assertNotIn(rel, report["conflicts"])
            self.assertNotIn(rel, [entry["path"] for entry in report["changes"]])
        self.assertEqual(len(custom), len(saved_paths))
        installer.restore(Path(report["backup"]))
        for rel, text in custom.items():
            self.assertEqual(text, (self.target / rel).read_text())

    def test_plain_install_keeps_prompts_and_installs_missing_prompt(self):
        custom = self.custom_prompts()
        missing = "adws/adw_data/prompt_engineering/reviewer/user.md"
        (self.target / missing).unlink()
        report = self.install()
        self.assertEqual([], report["conflicts"])
        self.assertEqual(self.prompts[missing], (self.target / missing).read_text())
        for rel, text in custom.items():
            if rel != missing:
                self.assertEqual(text, (self.target / rel).read_text())

    def test_dry_run_keeps_prompts_without_writing_comparison_copies(self):
        custom = self.custom_prompts()
        before = {path.relative_to(self.target): path.read_bytes()
                  for path in self.target.rglob("*") if path.is_file()}
        report = self.install(upgrade=True, dry_run=True)
        self.assertEqual(set(custom), {entry["path"] for entry in report["kept_project_files"]})
        self.assertTrue(all("package_version_saved_as" not in entry
                            for entry in report["kept_project_files"]))
        self.assertEqual(before, {path.relative_to(self.target): path.read_bytes()
                                 for path in self.target.rglob("*") if path.is_file()})

    def test_reset_config_still_keeps_project_prompts(self):
        custom = self.custom_prompts()
        config = self.target / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text(PROJECT_CONFIG)
        report = self.install(upgrade=True, reset_config=True)
        self.assertEqual("template configuration\n", config.read_text())
        self.assertEqual(set(custom), {entry["path"] for entry in report["kept_project_files"]})
        for rel, text in custom.items():
            self.assertEqual(text, (self.target / rel).read_text())

    def test_preserved_config_keeps_a_custom_prompt_destination_outside_default_prompt_folder(self):
        rel = "adws/adw_data/harness_engineering/custom-system.md"
        config = self.target / CONFIG
        config.parent.mkdir(parents=True, exist_ok=True)
        target = self.target / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        for reset in (False, True):
            with self.subTest(reset_config=reset):
                config.write_text(f"builder:\n  prompt_engineering:\n    system: '{rel}' # project prompt\n")
                target.write_text("custom F2B prompt\n")
                report = self.install(upgrade=True, reset_config=reset)
                self.assertEqual("custom F2B prompt\n", target.read_text())
                self.assertIn(rel, {entry["path"] for entry in report["kept_project_files"]})

    def test_inline_and_quoted_prompt_keys_preserve_configured_destinations_without_yaml_dependency(self):
        system = "adws/adw_data/harness_engineering/custom-system.md"
        user = "adws/adw_data/harness_engineering/custom-user.md"
        config = self.target / CONFIG
        config.parent.mkdir(parents=True)
        texts = (
            f"builder:\n  prompt_engineering: {{system: {system}, user: {user}}}\n",
            f"builder:\n  prompt_engineering: {{'system': '{system}', \"user\": \"{user}\"}}\n",
            f"builder:\n  prompt_engineering:\n    'system': '{system}'\n    \"user\": \"{user}\"\n",
        )
        for without_yaml in (False, True):
            for text in texts:
                with self.subTest(without_yaml=without_yaml, config=text), patch.dict(
                        sys.modules, {"yaml": None} if without_yaml else {}):
                    config.write_text(text)
                    for rel in (system, user):
                        target = self.target / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(f"project instructions for {rel}\n")
                    report = self.install(upgrade=True)
                    kept = {entry["path"] for entry in report["kept_project_files"]}
                    for rel in (system, user):
                        self.assertEqual(f"project instructions for {rel}\n", (self.target / rel).read_text())
                        self.assertIn(rel, kept)

    def test_yaml_alias_prompt_is_resolved_or_installation_stops_before_any_write(self):
        rel = "adws/adw_data/harness_engineering/custom-system.md"
        config = self.target / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text(f"shared: &shared '{rel}'\ncycle: &cycle {{self: *cycle}}\n"
                          "builder:\n  prompt_engineering: {system: *shared}\n")
        target = self.target / rel
        target.parent.mkdir(parents=True)
        target.write_text("project instructions\n")
        before = {path.relative_to(self.target): path.read_bytes()
                  for path in self.target.rglob("*") if path.is_file()}
        with patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaisesRegex(ValueError, "literal.*prompt"):
                self.install(upgrade=True)
        self.assertEqual(before, {path.relative_to(self.target): path.read_bytes()
                                 for path in self.target.rglob("*") if path.is_file()})
        report = self.install(upgrade=True)
        self.assertEqual("project instructions\n", target.read_text())
        self.assertIn(rel, {entry["path"] for entry in report["kept_project_files"]})

    def test_multiline_or_invalid_flow_yaml_without_dependency_stops_before_overwriting_prompt(self):
        rel = "adws/adw_data/harness_engineering/custom-system.md"
        config = self.target / CONFIG
        config.parent.mkdir(parents=True)
        target = self.target / rel
        target.parent.mkdir(parents=True)
        target.write_text("project instructions\n")
        for text in (
                f"builder:\n  prompt_engineering: {{\n    system: {rel},\n    user: other-user.md\n  }}\n",
                f"builder:\n  prompt_engineering: {{extra: {{}}, system: {rel},\n    user: {rel}}}\n",
                f"description: don't hide mappings\nbuilder:\n  prompt_engineering: {{\n"
                f"    system: {rel},\n    user: other-user.md\n  }}\nother: 'value'\n",
                f"builder:\n  prompt_engineering: {{system: {rel}]\n"):
            with self.subTest(config=text), patch.dict(sys.modules, {"yaml": None}):
                config.write_text(text)
                target.write_text("project instructions\n")
                before = {path.relative_to(self.target): path.read_bytes()
                          for path in self.target.rglob("*") if path.is_file()}
                with self.assertRaisesRegex(ValueError, "flow.*PyYAML"):
                    self.install(upgrade=True)
                self.assertEqual(before, {path.relative_to(self.target): path.read_bytes()
                                         for path in self.target.rglob("*") if path.is_file()})
        config.write_text(f"builder:\n  prompt_engineering: {{\n    system: {rel},\n"
                          "    user: other-user.md\n  }\n")
        report = self.install(upgrade=True)
        self.assertEqual("project instructions\n", target.read_text())
        self.assertIn(rel, {entry["path"] for entry in report["kept_project_files"]})


if __name__ == '__main__':
    unittest.main()
