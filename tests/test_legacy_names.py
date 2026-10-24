"""Ensure package names and namespaces are current."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]



class LegacyNameTests(unittest.TestCase):
    def test_migration_interface_is_removed(self):
        import sys
        import tempfile
        self.assertFalse((ROOT / 'scripts/migrate_legacy.py').exists())
        self.assertFalse((ROOT / '.claude/skills/kh/templates/adws/adw_modules/legacy.py').exists())
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'install.py'), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('--migrate-legacy', result.stdout)
        with tempfile.TemporaryDirectory() as target:
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'install.py'),
                                     '--project', target, '--upgrade', '--migrate-legacy'],
                                    capture_output=True, text=True)
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn('unrecognized arguments: --migrate-legacy', result.stderr)
            self.assertEqual([], list(Path(target).iterdir()))

    def test_current_namespace_keeps_its_effective_controls(self):
        import sys
        sys.path.insert(0, str(ROOT / '.claude/skills/kh/templates/adws'))
        from adw_modules import permissions, project_context
        from adw_modules.data_types import KHConfig
        self.assertEqual(('.claude', 'adws', 'vendor/delegate-skills', '.kh-backups'),
                         project_context.FRAMEWORK_PATHS)
        from tests.test_package import package
        for path in ('.kh/state', '.kh-observer/trace', '.kh-backups/originals/.env'):
            self.assertTrue(package.local_state(Path(path)), path)
        self.assertFalse(package.local_state(Path('src/legacy_name.py')))
        from types import SimpleNamespace
        agent = SimpleNamespace(writes=['**'])
        for path in ('.kh-backups', '.kh-backups/originals/key', '.kh/state', '.env'):
            self.assertFalse(permissions.permitted(path, agent, KHConfig()), path)
        self.assertTrue(permissions.permitted('src/value.py', agent, KHConfig()))

    def test_package_has_no_retired_namespace_names(self):
        if (ROOT / '.git').exists():
            names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        else:
            names = [line.split('  ', 1)[1] for line in (ROOT / 'MANIFEST.sha256').read_text().splitlines()]
            names.append('MANIFEST.sha256')
        unexpected = [name for name in names if name and (b's' * 3 + b'f') in (ROOT / name).read_bytes().lower()]
        self.assertEqual([], unexpected, 'no retired namespace names ship')


if __name__ == '__main__':
    unittest.main()
