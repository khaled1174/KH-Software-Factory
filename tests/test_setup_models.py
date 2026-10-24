"""Owner-recorded model/effort setup: validation, persistence and resolution."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADWS = ROOT / ".claude/skills/kh/templates/adws"
sys.path.insert(0, str(ADWS))
from adw_modules import agents, routing
from adw_modules.data_types import AgentConfig, ConfigDefaults, PromptEngineering, KHConfig
import adw_setup_models

SCRIPT = ADWS / "adw_setup_models.py"


class SetupModelsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="setup models ")
        self.root = Path(self.temp.name)
        self.cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)
        self.addCleanup(self.temp.cleanup)
        self.cfg = KHConfig(
            defaults=ConfigDefaults(required_quality_checks=["test"]),
            agents=[AgentConfig(name=role, coding_agent="claude_code", model=f"base-{role}",
                thinking="medium", prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
                for role in ("planner", "builder", "reviewer")])

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.root,
                              text=True, capture_output=True, timeout=30)

    def write_config(self, extra=None):
        data = {"defaults": {"required_quality_checks": ["test"]},
                "agents": [{"name": role, "coding_agent": "claude_code", "model": f"base-{role}",
                            "thinking": "medium",
                            "prompt_engineering": {"system": "s.md", "user": "u.md"}}
                           for role in ("planner", "builder", "reviewer")]}
        if extra: data.update(extra)
        path = self.root / "cfg.json"
        path.write_text(json.dumps(data))
        return path

    def test_new_role_partial_save_reports_readiness_and_preserves_limits(self):
        cfg = self.write_config()
        data = json.loads(cfg.read_text())
        for role in ("plan_reviewer", "project_architect"):
            data["agents"].append({**data["agents"][0], "name": role, "writes": []})
        cfg.write_text(json.dumps(data))
        result = self.invoke("--config", str(cfg), "--set",
                             "plan_reviewer=claude_code:critic:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"workflow_readiness"', result.stdout)
        loaded = agents.load_config(str(cfg))
        name, profile = routing.owner_profile(self.root)
        ready = routing.workflow_readiness(loaded, profile)
        self.assertTrue(ready["plan_review"]["configured"])
        self.assertFalse(ready["bootstrap"]["configured"])
        self.assertEqual(ready["bootstrap"]["missing_roles"], ["project_architect"])
        path = self.root / ".kh/models.json"
        record = json.loads(path.read_text())
        record.update(max_plan_repairs=1, max_bootstrap_repairs=4)
        path.write_text(json.dumps(record))
        result = self.invoke("--config", str(cfg), "--set",
                             "project_architect=claude_code:architect:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        name, profile = routing.owner_profile(self.root)
        self.assertEqual((profile.max_plan_repairs, profile.max_bootstrap_repairs), (1, 4))
        self.assertTrue(routing.workflow_readiness(loaded, profile)["first_start"]["configured"])
        resolved, decision = routing.resolve(loaded, self.root, available=lambda a: True,
                                            required_roles=routing.BOOTSTRAP_ROLES)
        self.assertEqual({a["role"] for a in decision["roles"]}, routing.BOOTSTRAP_ROLES)

    def test_setup_preserves_existing_empty_owner_profile_contract(self):
        cfg = self.write_config()
        path = self.root / '.kh/models.json'
        path.parent.mkdir()
        path.write_text('{"name":"owner"}')
        result = self.invoke('--config', str(cfg), '--set', 'builder=claude_code:chosen:medium', '--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(path.read_text())['roles']['builder'][0]['model'], 'chosen')

    def test_only_claude_and_codex_runtimes_are_available(self):
        self.assertEqual(set(routing.RUNTIMES), {'claude_code', 'delegate_codex'})
        self.assertEqual(set(routing.EFFORTS), {'claude_code', 'delegate_codex'})
        self.assertEqual(routing.RUNTIME_ALIASES, {})
        for name in ('agent_gemini.py', 'agent_pi.py'):
            self.assertFalse((ADWS / 'adw_modules' / name).exists(), name)
        for schema in (AgentConfig, ConfigDefaults, routing.Choice):
            self.assertEqual(set(schema.model_json_schema()['properties']['coding_agent']['enum']),
                             {'claude_code', 'delegate_codex'})
        self.assertEqual(ConfigDefaults().coding_agent, 'claude_code')
        for runtime in ('pi', 'gemini_cli'):
            with self.subTest(runtime=runtime), self.assertRaisesRegex(ValueError, 'retired.*?/kh-setup-models'):
                AgentConfig(**dict(self.cfg.agents[0].model_dump(), coding_agent=runtime))

    def test_retired_primary_repair_requires_explicit_new_effort_capabilities(self):
        cfg = self.write_config()
        path = self.root / '.kh/models.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'name':'owner','roles':{'builder':[
            dict(coding_agent='pi',model='historical',thinking='medium',supported_thinking=['medium','max'])]}}))
        before = path.read_bytes()
        args = ('--config', str(cfg), '--set', 'builder=delegate_codex:owner-codex:medium', '--apply')
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 1)
        self.assertIn('exceeds delegate_codex efforts', result.stderr)
        self.assertEqual(path.read_bytes(), before)
        result = self.invoke(*args, '--supported', 'builder=medium')
        self.assertEqual(result.returncode, 0, result.stderr)
        choice = json.loads(path.read_text())['roles']['builder'][0]
        self.assertEqual(choice['coding_agent'], 'delegate_codex')
        self.assertEqual(choice['supported_thinking'], ['medium'])
        self.assertIsNone(choice['tools'])

    def test_retired_antigravity_is_not_an_available_runtime(self):
        for name in ('agent_antigravity.py', 'antigravity_policy.py'):
            self.assertFalse((ADWS / 'adw_modules' / name).exists(), name)
        self.assertNotIn('antigravity_cli', routing.RUNTIMES)
        self.assertNotIn('antigravity', routing.RUNTIME_ALIASES)
        for schema in (AgentConfig, ConfigDefaults, routing.Choice):
            self.assertNotIn('antigravity_cli', schema.model_json_schema()['properties']['coding_agent']['enum'])
        self.assertNotIn('--tools', self.invoke('--help').stdout)
        with self.assertRaisesRegex(ValueError, 'retired.*?/kh-setup-models'):
            AgentConfig(**dict(self.cfg.agents[0].model_dump(), coding_agent='antigravity_cli'))

    def test_retired_owner_primary_and_fallback_fail_before_routing(self):
        from unittest.mock import Mock
        path = self.root / '.kh/models.json'
        path.parent.mkdir()
        supported = dict(coding_agent='claude_code', model='owner', thinking='medium',
                         supported_thinking=['medium'], tools=['read'])
        for retired_runtime in ('antigravity_cli', 'gemini_cli', 'pi'):
            retired = dict(supported, coding_agent=retired_runtime)
            for options in ([retired], [supported, retired]):
                with self.subTest(options=options):
                    path.write_text(json.dumps(dict(name='owner', roles={'builder': options})))
                    before = path.read_bytes()
                    available = Mock(return_value=True)
                    for overrides in (None, {'builder': routing.Choice(**supported)}):
                        with self.assertRaisesRegex(ValueError, r'(?s)models.json.*builder.*'+retired_runtime+r'.*\/kh-setup-models'):
                            routing.resolve(self.cfg, self.root, available=available, overrides=overrides)
                    available.assert_not_called()
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse((self.root / '.kh/routing.json').exists())

    def test_explicit_setup_repairs_retired_primary_without_losing_owner_settings(self):
        cfg = self.write_config()
        supported = dict(coding_agent='claude_code', model='backup', thinking='medium',
                         supported_thinking=['medium'], tools=['read', 'edit'])
        for retired_runtime in ('antigravity_cli', 'gemini_cli', 'pi'):
            record = dict(name='team', roles={'builder': [dict(supported, coding_agent=retired_runtime), supported],
                                            'reviewer': [supported]}, required_checks=['test', 'build'],
                          max_repairs=3, max_plan_repairs=1, max_bootstrap_repairs=4, note='Owner decision')
            path = self.root / '.kh/models.json'
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(record))
            before = path.read_bytes()
            args = ('--config', str(cfg), '--set', 'builder=claude_code:replacement:medium')
            preview = self.invoke(*args)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(path.read_bytes(), before)
            result = self.invoke(*args, '--apply')
            self.assertEqual(result.returncode, 0, result.stderr)
            after = json.loads(path.read_text())
            self.assertEqual(after['roles']['builder'][0], dict(supported, model='replacement'))
            self.assertEqual(after['roles']['builder'][1:], record['roles']['builder'][1:])
            self.assertEqual(after['roles']['reviewer'], record['roles']['reviewer'])
            for key in ('name', 'required_checks', 'max_repairs', 'max_plan_repairs', 'max_bootstrap_repairs', 'note'):
                self.assertEqual(after[key], record[key])
            resolved, _ = routing.resolve(agents.load_config(str(cfg)), self.root, available=lambda a: True)
            self.assertEqual(agents.resolve(resolved, 'builder').model, 'replacement')

    def test_setup_never_silently_removes_a_retired_fallback(self):
        cfg = self.write_config()
        choice = dict(coding_agent='claude_code', model='owner', thinking='medium', supported_thinking=['medium'])
        for retired_runtime in ('antigravity_cli', 'gemini_cli', 'pi'):
            path = self.root / '.kh/models.json'
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(dict(name='owner', roles={'builder': [choice, dict(choice, coding_agent=retired_runtime)]})))
            before = path.read_bytes()
            result = self.invoke('--config', str(cfg), '--set', 'builder=claude_code:new:medium', '--apply')
            self.assertEqual(result.returncode, 1)
            for word in ('models.json', 'builder', retired_runtime, '/kh-setup-models'):
                self.assertIn(word, result.stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((self.root / '.kh/routing.json').exists())

    def test_dry_run_writes_nothing(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg),
                             "--set", "builder=claude_code:opus:xhigh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"applied": false', result.stdout)
        self.assertFalse((self.root / ".kh/models.json").exists())
        self.assertFalse((self.root / ".kh/routing.json").exists())

    def test_apply_records_and_resolves_owner_choices(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg),
                             "--set", "planner=claude_code:opus:xhigh",
                             "--set", "builder=delegate_codex:owner-codex:low",
                             "--set", "reviewer=claude_code:sonnet:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.root / ".kh/models.json").read_text())
        self.assertEqual(record["name"], "owner")
        self.assertEqual(record["roles"]["builder"][0]["model"], "owner-codex")
        self.assertEqual(json.loads((self.root / ".kh/routing.json").read_text()), {"profile": "owner"})
        loaded = agents.load_config(str(cfg))
        resolved, decision = routing.resolve(loaded, self.root, available=lambda a: True)
        models = {a.name: (a.model, a.thinking) for a in resolved.agents}
        self.assertEqual(models["planner"], ("opus", "xhigh"))
        self.assertEqual(models["builder"], ("owner-codex", "low"))
        self.assertEqual(models["reviewer"], ("sonnet", "high"))
        self.assertEqual(decision["profile"], "owner")

    def test_partial_setup_leaves_other_roles_on_base(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg), "--set", "reviewer=claude_code:opus:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = agents.load_config(str(cfg))
        resolved, _ = routing.resolve(loaded, self.root, available=lambda a: True)
        models = {a.name: a.model for a in resolved.agents}
        self.assertEqual(models["planner"], "base-planner")
        self.assertEqual(models["reviewer"], "opus")

    def test_partial_update_preserves_existing_roles_and_profile_declarations(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg),
                             "--set", "planner=claude_code:owner-planner:high",
                             "--set", "builder=claude_code:owner-builder:high",
                             "--set", "reviewer=claude_code:owner-reviewer:high",
                             "--supported", "builder=medium,high", "--note", "Owner decision", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.root / ".kh/models.json"
        before = json.loads(path.read_text())
        before["required_checks"] = ["lint", "build"]
        before["max_repairs"] = 4
        before["roles"]["builder"][0]["tools"] = ["read", "edit"]
        before["roles"]["builder"].append(dict(before["roles"]["builder"][0], model="backup-builder"))
        before["roles"]["reviewer"].append(dict(before["roles"]["reviewer"][0], model="backup-reviewer"))
        path.write_text(json.dumps(before))
        result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:new-builder:medium", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(path.read_text())
        for field in ("required_checks", "max_repairs", "note"):
            self.assertEqual(after[field], before[field])
        for role in ("planner", "reviewer"):
            self.assertEqual(after["roles"][role], before["roles"][role])
        self.assertEqual(after["roles"]["builder"][0]["supported_thinking"], ["medium", "high"])
        self.assertEqual(after["roles"]["builder"][0]["tools"], ["read", "edit"])
        self.assertEqual(after["roles"]["builder"][1:], before["roles"]["builder"][1:])
        resolved, _ = routing.resolve(agents.load_config(str(cfg)), self.root, available=lambda a: True)
        self.assertEqual({a.name: a.model for a in resolved.agents}, {
            "planner": "owner-planner", "builder": "new-builder", "reviewer": "owner-reviewer"})

    def test_existing_capabilities_are_not_silently_widened(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:owner-builder:high",
                             "--supported", "builder=medium,high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        before = (self.root / ".kh/models.json").read_bytes()
        for apply in ([], ["--apply"]):
            result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:new-builder:xhigh", *apply)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("not in declared capabilities", result.stderr)
            self.assertEqual((self.root / ".kh/models.json").read_bytes(), before)

    def test_explicit_null_tools_survive_update_but_omitted_tools_stay_omitted(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg), "--set", "builder=delegate_codex:codex:high",
                             "--set", "planner=claude_code:planner:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.invoke("--config", str(cfg), "--set", "builder=delegate_codex:next-codex:medium", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        roles = json.loads((self.root / ".kh/models.json").read_text())["roles"]
        self.assertIn("tools", roles["builder"][0])
        self.assertIsNone(roles["builder"][0]["tools"])
        self.assertNotIn("tools", roles["planner"][0])

    def test_explicit_profile_rename_and_later_partial_update_keep_owner_settings(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg), "--profile", "team", "--set",
                             "planner=claude_code:owner-planner:high", "--note", "Original note", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.invoke("--config", str(cfg), "--profile", "team-renamed", "--set",
                             "builder=claude_code:owner-builder:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.invoke("--config", str(cfg), "--set", "reviewer=claude_code:owner-reviewer:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.root / ".kh/models.json").read_text())
        self.assertEqual(record["name"], "team-renamed")
        self.assertEqual(record["note"], "Original note")
        self.assertEqual(set(record["roles"]), {"planner", "builder", "reviewer"})
        self.assertEqual(routing.preference(self.root), "team-renamed")

    def test_unmatched_supported_declaration_is_rejected(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:builder:high",
                             "--supported", "reviewer=medium,high", "--apply")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("requires a matching --set", result.stderr)

    def test_invalid_untouched_choice_is_rejected_before_preview(self):
        cfg = self.write_config()
        self.invoke("--config", str(cfg), "--set", "planner=claude_code:planner:high", "--apply")
        path = self.root / ".kh/models.json"
        record = json.loads(path.read_text())
        record["roles"]["planner"][0]["supported_thinking"] = ["low"]
        path.write_text(json.dumps(record))
        before = path.read_bytes()
        result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:builder:high")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertEqual(path.read_bytes(), before)







    def test_setup_preview_declares_local_effects_and_never_launches_provider(self):
        cfg = self.write_config()
        provider = self.root / "claude-fixture"
        called = self.root / "provider-called"
        provider.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                            f"Path({str(called)!r}).write_text('called')\nraise SystemExit(99)\n")
        provider.chmod(0o755)
        with patch.dict(os.environ, {"CLAUDE_CODE_PATH": str(provider)}):
            for apply in (False, True):
                result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:fixture:medium",
                                     *(["--apply"] if apply else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                preview, _ = json.JSONDecoder().raw_decode(result.stdout)
                self.assertEqual(preview["effects"]["provider_processes"], 0)
                self.assertEqual(preview["effects"]["network_calls"], 0)
                self.assertEqual(preview["effects"]["paths"], [".kh/models.json", ".kh/routing.json"])
                self.assertEqual(preview["effects"]["written_paths"],
                                 [".kh/models.json", ".kh/routing.json"] if apply else [])
                self.assertFalse(called.exists(), "configuration must never start a provider")

    def test_retired_live_model_flags_are_rejected_before_writes(self):
        cfg = self.write_config()
        for args in (["--next-call"], ["--run-id", "worker"],
                     ["--next-call", "--run-id", "worker"]):
            with self.subTest(args=args):
                result = self.invoke("--config", str(cfg), "--set",
                                     "builder=claude_code:new:medium", *args, "--apply")
                self.assertEqual(result.returncode, 2)
                self.assertIn("unrecognized arguments", result.stderr)
                self.assertFalse((self.root / ".kh").exists())
        result = subprocess.run([sys.executable, str(ADWS / "adw_models.py"),
                                 "--config", str(cfg), "--live", "--run-id", "worker"],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertFalse((self.root / ".kh").exists())

    def test_default_apply_refuses_active_writer_and_persists_after_release(self):
        import select
        cfg = self.write_config()
        args = ["--config", str(cfg), "--set", "builder=claude_code:before-run:medium", "--apply"]
        initial = self.invoke(*args)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        paths = [self.root / ".kh/models.json", self.root / ".kh/routing.json"]
        before = [path.read_bytes() for path in paths]
        script = self.root / "writer.py"
        script.write_text("import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0,{str(ADWS)!r})\n"
            "from adw_modules import session\n"
            "with session.acquire_repo_lock(Path(sys.argv[1])):\n"
            " print('ready',flush=True)\n sys.stdin.readline()\n")
        worker = subprocess.Popen([sys.executable, str(script), str(self.root)], cwd=self.root,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        def cleanup():
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
        self.addCleanup(cleanup)
        try:
            self.assertTrue(select.select([worker.stdout], [], [], 5)[0], "writer did not become ready")
            self.assertEqual(worker.stdout.readline().strip(), "ready")
            denied = self.invoke("--config", str(cfg), "--set",
                                 "builder=claude_code:after-run:medium", "--apply")
            self.assertEqual(denied.returncode, 1)
            self.assertIn("active", denied.stderr)
            self.assertEqual(before, [path.read_bytes() for path in paths])
            self.assertNotIn("--next-call", denied.stderr)
        finally:
            if worker.poll() is None:
                output, error = worker.communicate("continue\n", timeout=5)
                self.assertEqual(worker.returncode, 0, output + error)
        applied = self.invoke("--config", str(cfg), "--set",
                              "builder=claude_code:after-run:medium", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        saved = json.loads(paths[0].read_text())
        self.assertEqual(saved["roles"]["builder"][0]["model"], "after-run")

    def test_recorded_owner_choice_reaches_explicit_prompt_builder(self):
        import adw_prompt
        cfg = self.write_config()
        for prompt in ("s.md", "u.md"):
            (self.root / prompt).write_text("{{prompt}}")
        result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:fixture:medium", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(routing, "binary_status", return_value={"fixture": "/fixture"}), \
             patch.object(agents.agent_cc, "preflight", return_value=[]), \
             patch.object(agents.agent_cc, "resolve_model", return_value="sonnet"), \
             patch.object(adw_prompt.session, "ensure", side_effect=RuntimeError("captured-config")) as ensure:
            with self.assertRaisesRegex(RuntimeError, "captured-config"):
                adw_prompt.main("bounded request", agent='builder', config=str(cfg))
        effective = ensure.call_args.args[0]
        builder = agents.resolve(effective, "builder")
        self.assertEqual((builder.coding_agent, builder.model, builder.thinking),
                         ("claude_code", "fixture", "medium"))
        self.assertEqual(agents.resolve(effective, "planner").coding_agent, "claude_code")


    def test_unknown_role_rejected(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg), "--set", "scout=claude_code:haiku:low", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a routing role", result.stderr)
        self.assertFalse((self.root / ".kh/models.json").exists())

    def test_unsupported_effort_rejected(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg), "--set", "builder=delegate_codex:codex:max", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not supported by delegate_codex", result.stderr)
        self.assertFalse((self.root / ".kh/models.json").exists())

    def test_effort_outside_declared_capabilities_rejected(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg),
                             "--set", "builder=claude_code:opus:xhigh",
                             "--supported", "builder=medium,high", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not in declared capabilities", result.stderr)

    def test_codex_switch_records_explicit_null_tools(self):
        cfg = self.write_config()
        result = self.invoke("--config", str(cfg), "--set", "builder=delegate_codex:codex:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.root / ".kh/models.json").read_text())
        choice = record["roles"]["builder"][0]
        self.assertIn("tools", choice)
        self.assertIsNone(choice["tools"])
        loaded = agents.load_config(str(cfg))
        resolved, _ = routing.resolve(loaded, self.root, available=lambda a: True)
        builder = next(a for a in resolved.agents if a.name == "builder")
        self.assertEqual(builder.coding_agent, "delegate_codex")
        self.assertIsNone(builder.tools)

    def test_explicit_codex_switch_uses_native_tools_and_preserves_source_scope(self):
        cfg = self.write_config()
        data = json.loads(cfg.read_text())
        builder = next(a for a in data["agents"] if a["name"] == "builder")
        builder["tools"] = ["Read", "Edit"]
        builder["writes"] = ["src/"]
        cfg.write_text(json.dumps(data))
        result = self.invoke("--config", str(cfg), "--set", "builder=delegate_codex:codex:high", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded, _ = routing.resolve(agents.load_config(str(cfg)), self.root, available=lambda a: True)
        selected = next(a for a in loaded.agents if a.name == "builder")
        self.assertIsNone(selected.tools)
        self.assertEqual(selected.writes, ["src/"])

    def test_profile_name_collision_with_config_rejected(self):
        cfg = self.write_config(extra={"routing": {"default_profile": "current",
                                                   "profiles": {"current": {}, "owner": {}}}})
        result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:opus:high", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)

    def test_profile_collision_is_rejected_in_dry_run_before_preview(self):
        cfg = self.write_config(extra={"routing": {"default_profile": "current",
                                                   "profiles": {"current": {}, "owner": {}}}})
        result = self.invoke("--config", str(cfg), "--set", "builder=claude_code:opus:high")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_failed_preference_write_restores_previous_owner_record(self):
        cfg = self.write_config()
        self.invoke("--config", str(cfg), "--set", "builder=claude_code:owner-builder:high", "--apply")
        path = self.root / ".kh/models.json"
        before = json.loads(path.read_text())
        updated = dict(before, note="New note")
        with patch.object(routing, "save_preference", side_effect=OSError("fixture disk failure")):
            with self.assertRaisesRegex(OSError, "fixture disk failure"):
                adw_setup_models.persist_record(self.root, updated, agents.load_config(str(cfg)))
        self.assertEqual(json.loads(path.read_text()), before)
        self.assertEqual(routing.preference(self.root), "owner")

    def test_stale_default_save_cannot_drop_a_concurrent_role_update(self):
        cfg = self.write_config()
        first_record = {"name": "owner", "roles": {"builder": [{
            "coding_agent": "claude_code", "model": "first-builder", "thinking": "medium",
            "supported_thinking": ["medium"]}]}, "max_repairs": 4, "note": "preserve this"}
        persist = adw_setup_models.persist_record
        def interleaved(root, second_record, loaded_cfg, **kwargs):
            # Request A saves after B read the empty owner record and before
            # B acquires the same writer lease to persist its full preview.
            persist(root, first_record, loaded_cfg)
            return persist(root, second_record, loaded_cfg, **kwargs)
        argv = [str(SCRIPT), "--config", str(cfg), "--set", "reviewer=claude_code:second-reviewer:medium", "--apply"]
        with patch.object(sys, "argv", argv), patch.object(adw_setup_models, "persist_record", side_effect=interleaved), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "STALE_MODEL_SETUP"):
                adw_setup_models.main()
        after = json.loads((self.root / ".kh/models.json").read_text())
        self.assertEqual(after, first_record)
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            self.assertEqual(adw_setup_models.main(), 0)
        after = json.loads((self.root / ".kh/models.json").read_text())
        self.assertEqual(set(after["roles"]), {"builder", "reviewer"})
        self.assertEqual((after["max_repairs"], after["note"]), (4, "preserve this"))

    def test_failed_initial_selection_leaves_no_partial_owner_record(self):
        cfg = self.write_config()
        record = {"name": "owner", "roles": {}}
        with patch.object(routing, "save_preference", side_effect=OSError("fixture disk failure")):
            with self.assertRaisesRegex(OSError, "fixture disk failure"):
                adw_setup_models.persist_record(self.root, record, agents.load_config(str(cfg)))
        self.assertFalse((self.root / ".kh/models.json").exists())
        self.assertFalse((self.root / ".kh/routing.json").exists())

    def test_failure_after_selection_write_rolls_back_both_records(self):
        cfg = self.write_config()
        self.invoke("--config", str(cfg), "--set", "builder=claude_code:owner-builder:high", "--apply")
        path = self.root / ".kh/models.json"
        before = json.loads(path.read_text())
        updated = dict(before, name="renamed")
        save = routing.save_preference
        def fail_after_write(root, name, config):
            save(root, name, config)
            raise OSError("fixture post-write failure")
        with patch.object(routing, "save_preference", side_effect=fail_after_write):
            with self.assertRaisesRegex(OSError, "fixture post-write failure"):
                adw_setup_models.persist_record(self.root, updated, agents.load_config(str(cfg)))
        self.assertEqual(json.loads(path.read_text()), before)
        self.assertEqual(routing.preference(self.root), "owner")

    def test_malformed_owner_file_fails_closed(self):
        (self.root / ".kh").mkdir()
        (self.root / ".kh/models.json").write_text('{"name": "owner", "roles": {"chef": []}}')
        with self.assertRaises(Exception):
            routing.resolve(self.cfg, self.root, available=lambda a: True)

    def test_owner_profile_beats_saved_yaml_default(self):
        cfg = self.write_config()
        self.invoke("--config", str(cfg), "--set", "builder=claude_code:opus:high", "--apply")
        loaded = agents.load_config(str(cfg))
        name = routing.choose_profile(loaded, self.root)
        self.assertEqual(name, "owner")


if __name__ == "__main__":
    unittest.main()
