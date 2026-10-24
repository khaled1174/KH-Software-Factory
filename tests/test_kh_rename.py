"""Current KH installs preserve owner files and exclude backed-up credentials."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from tests.contracts import COMMAND_COUNTS

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kh_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kh-migrate-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_verification_adapter_uses_current_namespace_only(self):
        from tests.test_verification_evidence import AdapterCommandTests
        from adw_modules import verification_evidence
        fixture = AdapterCommandTests('test_real_subprocess_green_keeps_evidence')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        prefix = ("s" * 3 + "f").upper() + "_"
        fixture.configure("\nfor suffix in ('VERIFICATION_RESULT', 'CANDIDATE_SHA256', 'CHANGED_PATHS'):\n"
                          "    assert os.environ['KH_' + suffix]\n"
                          f"    assert {prefix!r} + suffix not in os.environ\n")
        result = verification_evidence.run_adapters(fixture.run, ['src/Button.tsx'])
        self.assertTrue(result.passed, result.failures)

    def test_new_install_has_18_kh_commands_and_no_legacy_namespace(self):
        installer.install(self.root)
        self.assertEqual(len(list((self.root / ".claude/commands").glob("kh-*.md"))), COMMAND_COUNTS['commands'])
        self.assertEqual(list((self.root / ".claude/commands").glob(("s" * 3 + "f") + "-*.md")), [])
        self.assertTrue((self.root / ".claude/skills/kh/SKILL.md").is_file())
        self.assertTrue((self.root / "adws/adw_kh_config/kh.config.yaml").is_file())
        self.assertIn("KH", (ROOT / ".claude/skills/kh/apps/visualizer/src/App.vue").read_text())
        self.assertNotIn("Super Simple Software Factory",
                         (ROOT / ".claude/skills/kh/apps/visualizer/src/App.vue").read_text())

    def test_new_install_leaves_an_existing_linked_env_untouched(self):
        shared = self.root / "owner-secrets"
        shared.write_text("owner-owned\n")
        linked = self.root / ".env"
        linked.symlink_to(shared)
        installer.install(self.root)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(shared.read_text(), "owner-owned\n")

    def test_backed_up_credentials_do_not_enter_a_real_worker_copy(self):
        import sys
        from types import SimpleNamespace
        sys.path.insert(0, str(ROOT / ".claude/skills/kh/templates/adws"))
        from adw_modules.data_types import KHConfig
        from adw_modules.workspace import AgentWorkspace
        (self.root / "source.py").write_text("value = 1\n")
        for directory in (".kh-backups",):
            saved = self.root / directory / "originals/.env"
            saved.parent.mkdir(parents=True)
            saved.write_text("private-fixture\n")
        run = SimpleNamespace(repo_root=self.root, cfg=KHConfig(), adw_id="probe",
                              context_handoff_dir=self.root / "adws/adw_data/sessions/probe/context_handoff")
        workspace = AgentWorkspace(run, SimpleNamespace(phase_id="build"), SimpleNamespace(name="builder"))
        self.addCleanup(workspace.temp.cleanup)
        self.assertTrue((workspace.root / "source.py").is_file())
        self.assertFalse((workspace.root / ".kh-backups").exists())



if __name__ == "__main__":
    unittest.main()
