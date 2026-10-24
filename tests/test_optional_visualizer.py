"""Optional UI installation must not widen core upgrades or rollback scope."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from tests.contracts import COMMAND_COUNTS

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('optional_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
UI = Path('.claude/skills/kh/apps/visualizer')


class OptionalVisualizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kh optional ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_trace_only_visualizer_keeps_navigation_and_one_dependency_lock(self):
        installer.install(self.root, with_visualizer=True)
        app = self.root / UI
        for component in ('ProjectOverview', 'SessionsList', 'SessionTrace', 'PhaseDetail'):
            self.assertTrue((app / 'src/components' / (component + '.vue')).is_file())
        self.assertFalse((app / 'src/components/ExecutionBoard.vue').exists())
        self.assertFalse((self.root / 'adws/adw_modules/kanban.py').exists())
        self.assertTrue((app / 'bun.lock').is_file())
        self.assertFalse((app / 'package-lock.json').exists())
        self.assertFalse(list((app / 'public').rglob('*.png')))
        workflow = (ROOT / '.github/workflows/verify.yml').read_text()
        self.assertIn('"$KH_BUN" install --frozen-lockfile', workflow)
        self.assertNotIn('npm ci', workflow)

    def test_fresh_core_and_text_traces_have_no_visualizer(self):
        report = installer.install(self.root)
        self.assertEqual(['core'], report['components'])
        self.assertFalse((self.root / UI).exists())
        self.assertTrue((self.root / 'adws/kh_commands.py').is_file())
        result = subprocess.run([sys.executable, str(self.root / 'adws/kh_commands.py'), '--help'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(COMMAND_COUNTS['commands'], len(list((self.root / '.claude/commands').glob('kh-*.md'))))
        self.assertTrue((self.root / 'vendor/delegate-skills').is_dir())

    def test_add_component_idempotent_and_restore_only_component(self):
        installer.install(self.root)
        report = installer.install(self.root, with_visualizer=True)
        self.assertTrue((self.root / UI / 'package.json').is_file())
        self.assertFalse((self.root / UI / 'node_modules').exists())
        self.assertTrue(report['changes'])
        self.assertTrue(all(entry['path'].startswith(UI.as_posix() + '/') for entry in report['changes']))
        self.assertEqual([], installer.install(self.root, with_visualizer=True)['changes'])
        manifest = json.loads((Path(report['backup']) / 'restore.json').read_text())
        self.assertEqual(['core', 'visualizer'], manifest['components'])
        installer.restore(Path(report['backup']))
        self.assertFalse((self.root / UI / 'package.json').exists())
        self.assertTrue((self.root / 'adws/adw_start.py').is_file())

    def test_core_upgrade_and_restore_preserve_customized_visualizer(self):
        installer.install(self.root, with_visualizer=True)
        owner_ui = self.root / UI / 'package.json'
        owner_ui.write_text('owner customization')
        core = self.root / 'adws/adw_start.py'
        core.write_text('owner runtime')
        report = installer.install(self.root, upgrade=True)
        self.assertEqual('owner customization', owner_ui.read_text())
        self.assertFalse(any(c['path'].startswith(UI.as_posix() + '/') for c in report['changes']))
        installer.restore(Path(report['backup']))
        self.assertEqual('owner customization', owner_ui.read_text())
        self.assertEqual('owner runtime', core.read_text())

    def test_explicit_component_upgrade_requires_normal_conflict_handling(self):
        installer.install(self.root, with_visualizer=True)
        path = self.root / UI / 'package.json'
        path.write_text('owner UI')
        with self.assertRaises(ValueError):
            installer.install(self.root, with_visualizer=True)
        self.assertEqual('owner UI', path.read_text())
        report = installer.install(self.root, with_visualizer=True, upgrade=True)
        installer.restore(Path(report['backup']))
        self.assertEqual('owner UI', path.read_text())

    def test_dry_run_optional_component_and_user_scope(self):
        installer.install(self.root, with_visualizer=True, dry_run=True)
        self.assertEqual([], list(self.root.iterdir()))
        with self.assertRaises(ValueError):
            installer.install(self.root, user=True, with_visualizer=True)
        self.assertEqual([], list(self.root.iterdir()))

    def test_skill_installer_core_then_optional_source(self):
        script = ROOT / '.claude/skills/kh/scripts/install.py'
        for options in ([], ['--with-visualizer'], ['--with-visualizer']):
            result = subprocess.run([sys.executable, str(script), *options], cwd=self.root,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(bool(options), (self.root / UI / 'package.json').is_file())
        self.assertFalse((self.root / UI / 'node_modules').exists())

    def _staged_package(self) -> Path:
        """A package copy that has been developed in: built UI and installed deps.

        The suite otherwise reads the live checkout, so whether these paths
        exist depends on what the developer last ran. Staging them makes the
        boundary a stated contract instead of a property of someone's disk.
        """
        stage = Path(tempfile.mkdtemp(prefix='kh package '))
        self.addCleanup(shutil.rmtree, stage, True)
        package = stage / 'package'
        shutil.copytree(ROOT, package, symlinks=True,
                        ignore=shutil.ignore_patterns('node_modules', 'dist', '.git', '__pycache__'))
        built = package / UI / 'dist/assets'
        built.mkdir(parents=True)
        (built / 'index-stale.js').write_text('built on the maintainer machine')
        binaries = package / UI / 'node_modules/.bin'
        binaries.mkdir(parents=True)
        (package / UI / 'node_modules/vite/bin').mkdir(parents=True)
        (package / UI / 'node_modules/vite/bin/vite.js').write_text('#!/usr/bin/env node\n')
        os.symlink('../vite/bin/vite.js', binaries / 'vite')
        return package

    def test_developed_package_ships_no_build_input_or_output(self):
        report = installer.install(self.root, package=self._staged_package(), with_visualizer=True)
        self.assertTrue((self.root / UI / 'package.json').is_file())
        self.assertFalse((self.root / UI / 'dist').exists())
        self.assertFalse((self.root / UI / 'node_modules').exists())
        self.assertFalse(any('/dist/' in entry['path'] or '/node_modules/' in entry['path']
                             for entry in report['changes']))

    def test_installed_ui_dependencies_do_not_block_a_later_component_install(self):
        script = ROOT / '.claude/skills/kh/scripts/install.py'
        subprocess.run([sys.executable, str(script), '--with-visualizer'], cwd=self.root,
                       capture_output=True, text=True, check=True)
        binaries = self.root / UI / 'node_modules/.bin'
        binaries.mkdir(parents=True)
        (self.root / UI / 'node_modules/vite/bin').mkdir(parents=True)
        (self.root / UI / 'node_modules/vite/bin/vite.js').write_text('#!/usr/bin/env node\n')
        os.symlink('../vite/bin/vite.js', binaries / 'vite')
        (self.root / UI / 'dist').mkdir()
        os.symlink('../package.json', self.root / UI / 'dist/linked-asset.json')
        again = subprocess.run([sys.executable, str(script), '--with-visualizer'], cwd=self.root,
                               capture_output=True, text=True)
        self.assertEqual(0, again.returncode, again.stderr)
        # A link in content that IS copied still refuses before anything is written.
        os.symlink('package.json', self.root / UI / 'linked-source.json')
        refused = subprocess.run([sys.executable, str(script), '--with-visualizer'], cwd=self.root,
                                 capture_output=True, text=True)
        self.assertEqual(2, refused.returncode)
        self.assertIn('symlink paths are not supported', refused.stderr)

    def test_obs_fails_helpfully_before_bun_when_ui_missing(self):
        import shutil
        bash = shutil.which('bash')
        if not bash:
            self.skipTest('Bash is needed for the shipped obs recipe')
        recipe = (ROOT / '.claude/skills/kh/templates/justfile').read_text().split('\nobs:\n', 1)[1]
        script = '\n'.join(line[4:] if line.startswith('    ') else line for line in recipe.splitlines())
        result = subprocess.run([bash, '-c', script], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('--with-visualizer', result.stderr)
        self.assertIn('just sessions', result.stderr)
        self.assertNotIn('command not found', result.stderr)


if __name__ == '__main__':
    unittest.main()
