"""Controller -> isolated workspace -> real relay -> fake provider -> promotion."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude/skills/kh/templates/adws"))
from adw_modules import agents, permissions
from adw_modules.data_types import (AgentCall, AgentConfig, BuildOutput, Phase,
                                    PhaseParams, PromptEngineering, KHConfig)
from adw_modules.workspace import AgentWorkspace


@unittest.skipUnless(os.name == "posix" and shutil.which("node") and shutil.which("git"),
                     "POSIX, Node and Git required")
class DelegateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delegate integration ")
        self.base = Path(self.temp.name)
        self.source = self.base / "repo"
        self.source.mkdir()
        (self.source / "src").mkdir()
        (self.source / "src/allowed.py").write_text("owner original\n")
        (self.source / "private.txt").write_text("keep owner data\n")
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm", "initial"], check=True)
        (self.source / "system.md").write_text("Implement the explicit task")
        (self.source / "user.md").write_text("{prompt}")
        self.agent = AgentConfig(name="builder", coding_agent="delegate_codex", model="fixture-model",
                                 writes=["src/allowed.py"], prompt_engineering=PromptEngineering(
                                     system=str(self.source / "system.md"), user=str(self.source / "user.md")))
        cfg = KHConfig(agents=[self.agent])
        session = self.source / "adws/adw_data/sessions/run"
        handoff = session / "context_handoff"
        handoff.mkdir(parents=True)
        self.run = SimpleNamespace(repo_root=self.source, cfg=cfg, adw_id="run", session_dir=session,
                                   context_handoff_dir=handoff, tracer=Mock(), console=Mock(),
                                   agent_map={}, add_usage=Mock(), save_agent_map=Mock(),
                                   required_requirements=[])
        self.phase = Phase(phase_id="delegate", adw_id="run", seq=0,
                           params=PhaseParams(name="build", kind="agent", owner="builder",
                                              description="Implement fixture through the Codex relay"))
        binary = self.base / "bin"
        binary.mkdir()
        cli = binary / "codex"
        cli.write_text('''#!/usr/bin/env python3
import json,os,re,sys
from pathlib import Path
if "--version" in sys.argv:
    print("codex-cli fixture-0"); sys.exit(0)
brief = json.load(sys.stdin)
Path("src/allowed.py").write_text("delegated change\\n")
mode = os.environ.get("KH_TEST_INTEGRATION_MODE", "good")
if mode == "unauthorized":
    Path("private.txt").write_text("worker must not change this\\n")
evidence = Path(re.search(r"Write evidence only under (.+)", brief["instructions"])[1]) / "evidence.md"
evidence.write_text("Fixture verification evidence\\n")
final = {"status":"success","summary":"Changed source", "changed_files":["src/allowed.py"],
         "artifacts":[str(evidence)]}
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(final))
print(json.dumps({"type":"thread.started","thread_id":"fixture-thread"}))
if mode == "provider-error":
    print(json.dumps({"type":"error","message":"provider failure after write"}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}))
''')
        cli.chmod(0o755)
        self.env = {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, mode="good"):
        with patch.dict(os.environ, {**self.env, "KH_TEST_INTEGRATION_MODE": mode}):
            return agents.execute(self.run, self.phase,
                                  AgentCall(output_type=BuildOutput, prompt="Change src/allowed.py"))

    def test_success_promotes_source_and_evidence(self):
        result = self.execute()
        self.assertEqual((self.source / "src/allowed.py").read_text(), "delegated change\n")
        self.assertEqual((self.source / "private.txt").read_text(), "keep owner data\n")
        self.assertEqual(result.changed_files, ["src/allowed.py"])
        self.assertEqual(len(result.artifacts), 1)
        artifact = Path(result.artifacts[0])
        self.assertTrue(artifact.is_relative_to(self.source))
        self.assertIn("Fixture verification", artifact.read_text())
        self.run.add_usage.assert_called_with(15, 0.0, False)
        self.run.save_agent_map.assert_called_once()
        receipts = list(self.run.session_dir.glob("builder/phases/delegate/attempt-*/relay/delegate-*/normalized.json"))
        self.assertEqual(len(receipts), 1)
        self.assertFalse(json.loads(receipts[0].read_text())["accepted"])

    def test_unauthorized_delta_discards_all_worker_changes(self):
        with self.assertRaisesRegex(permissions.PermissionBreach, "private.txt"):
            self.execute("unauthorized")
        self.assert_owner_unchanged()

    def test_provider_failure_after_write_prevents_promotion(self):
        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            self.execute("provider-error")
        self.assert_owner_unchanged()

    def assert_owner_unchanged(self):
        self.assertEqual((self.source / "src/allowed.py").read_text(), "owner original\n")
        self.assertEqual((self.source / "private.txt").read_text(), "keep owner data\n")
        self.assertFalse(list(self.run.context_handoff_dir.rglob("evidence.md")))

    def test_existing_absolute_internal_symlink_is_remapped(self):
        (self.source / "alias.txt").symlink_to(self.source / "src/allowed.py")
        with AgentWorkspace(self.run, self.phase, self.agent) as work:
            (work.root / "alias.txt").write_text("isolated alias change")
            self.assertEqual((work.root / "src/allowed.py").read_text(), "isolated alias change")
            self.assertEqual((self.source / "src/allowed.py").read_text(), "owner original\n")

    def test_external_input_symlink_rejected_before_dispatch(self):
        external = self.base / "outside.txt"
        external.write_text("outside owner data")
        (self.source / "alias.txt").symlink_to(external)
        with self.assertRaisesRegex(permissions.PermissionBreach, "symlink escapes"):
            AgentWorkspace(self.run, self.phase, self.agent)
        self.assertEqual(external.read_text(), "outside owner data")


if __name__ == "__main__":
    unittest.main()
