"""Owner-controlled routing without provider calls or authentication claims."""
import os as _os, tempfile as _tempfile, pathlib as _pathlib  # macOS: the temp root (/var/...) is a symlink;
_tempfile.tempdir = _os.environ['TMPDIR'] = str(_pathlib.Path(_tempfile.gettempdir()).resolve())  # KH refuses symlinked state paths
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude/skills/kh/templates/adws"))
from adw_modules import routing
from adw_modules.data_types import AgentConfig, ConfigDefaults, PromptEngineering, KHConfig


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="routing project ")
        self.root = Path(self.temp.name)
        self.cfg = KHConfig(
            defaults=ConfigDefaults(required_quality_checks=["test", "lint"]),
            agents=[AgentConfig(name=role, coding_agent="claude_code", model=f"base-{role}",
                tools=["Read"] if role != "builder" else ["Read", "Edit"],
                writes=["src/**"] if role == "builder" else [],
                prompt_engineering=PromptEngineering(system="system.md", user="user.md"))
                for role in ("planner", "builder", "reviewer")],
            routing={"default_profile": "current", "profiles": {
                "current": {}, "alternate": {"roles": {"builder": [self.choice("alternate")]}},
            }},
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def choice(model, **updates):
        return {"coding_agent": "claude_code", "model": model, "thinking": "high",
                "supported_thinking": ["medium", "high"], **updates}

    def profile(self, choices, **updates):
        self.cfg.routing["profiles"]["current"] = {"roles": {"builder": choices}, **updates}

    def test_new_workflows_require_explicit_choices_before_availability(self):
        available = Mock(return_value=True)
        with self.assertRaisesRegex(ValueError, "plan_reviewer.*kh-setup-models"):
            routing.resolve(self.cfg, self.root, available=available,
                            required_roles=routing.REVIEWED_ROLES)
        available.assert_not_called()
        # A scaffolded agent is not an owner model decision.
        self.cfg.agents.append(self.cfg.agents[0].model_copy(update={"name": "plan_reviewer"}))
        with self.assertRaisesRegex(ValueError, "plan_reviewer"):
            routing.resolve(self.cfg, self.root, available=available,
                            required_roles={"planner", "plan_reviewer"})
        available.assert_not_called()

    def test_plan_only_resolves_only_required_roles_and_independent_budgets(self):
        self.cfg.agents.append(self.cfg.agents[0].model_copy(update={"name": "plan_reviewer"}))
        self.cfg.routing["profiles"]["current"] = {
            "roles": {"plan_reviewer": [self.choice("critic")]},
            "max_repairs": 4, "max_plan_repairs": 1, "max_bootstrap_repairs": 3}
        available = Mock(return_value=True)
        resolved, decision = routing.resolve(self.cfg, self.root, available=available,
                                             required_roles={"planner", "plan_reviewer"})
        self.assertEqual({c.args[0].name for c in available.call_args_list}, {"planner", "plan_reviewer"})
        self.assertEqual(decision["max_repairs"], 4)
        self.assertEqual(decision["max_plan_repairs"], 1)
        self.assertEqual(decision["max_bootstrap_repairs"], 3)
        self.assertEqual(next(a for a in resolved.agents if a.name == "plan_reviewer").writes, [])

    def test_new_budgets_fail_closed_and_old_profiles_default(self):
        for schema in (routing.Profile, routing.OwnerModels):
            base = {"name": "owner"} if schema is routing.OwnerModels else {}
            parsed = schema.model_validate(base)
            self.assertEqual((parsed.max_plan_repairs, parsed.max_bootstrap_repairs), (2, 2))
            for key in ("max_plan_repairs", "max_bootstrap_repairs"):
                for value in (-1, 6, True, 1.5, "2"):
                    with self.subTest(schema=schema, key=key, value=value):
                        with self.assertRaises(ValueError):
                            schema.model_validate({**base, key: value})

    def test_unknown_required_role_rejected(self):
        with self.assertRaisesRegex(ValueError, "required_roles"):
            routing.resolve(self.cfg, self.root, required_roles={"chef"})

    def test_ordered_fallback_uses_first_available_owner_choice(self):
        self.profile([self.choice("missing"), self.choice("second"), self.choice("third")])
        available = Mock(side_effect=lambda agent: agent.model != "missing")
        resolved, decision = routing.resolve(self.cfg, self.root, available=available)
        builder = next(a for a in resolved.agents if a.name == "builder")
        self.assertEqual(builder.model, "second")
        models = [call.args[0].model for call in available.call_args_list]
        self.assertNotIn("third", models)
        row = next(row for row in decision["roles"] if row["role"] == "builder")
        self.assertTrue(row["fallback"])
        self.assertEqual(row["requested"]["model"], "missing")
        self.assertEqual(row["selected"]["model"], "second")
        self.assertEqual(row["authentication"], "unverified")

    def test_all_unavailable_choices_fail_without_invented_fallback(self):
        self.profile([self.choice("one"), self.choice("two")])
        available = Mock(return_value=False)
        with self.assertRaisesRegex(RuntimeError, "No configured available runtime"):
            routing.resolve(self.cfg, self.root, available=available)
        self.assertEqual([c.args[0].model for c in available.call_args_list], ["one", "two"])




    def test_legacy_validation_applies_only_required_owner_roles_and_keeps_scope(self):
        routing._atomic_json(routing.owner_models_path(self.root), {
            "name": "owner", "roles": {
                "builder": [self.choice("gemini-3.8-flash", coding_agent="claude_code", tools=["read", "edit"])],
                "planner": [self.choice("unavailable-planner")]}, "required_checks": ["build"]})
        routing.save_preference(self.root, "owner", self.cfg)
        before = self.cfg.model_dump()
        available = Mock(return_value=True)
        routing.apply_legacy_choices(self.cfg, self.root, ["builder"], available=available)
        self.assertEqual([c.args[0].name for c in available.call_args_list], ["builder"])
        builder = next(a for a in self.cfg.agents if a.name == "builder")
        self.assertEqual((builder.coding_agent, builder.model, builder.tools),
                         ("claude_code", "gemini-3.8-flash", ["read", "edit"]))
        self.assertEqual(builder.writes, ["src/**"])
        self.assertEqual(next(a for a in self.cfg.agents if a.name == "planner").model, "base-planner")
        self.assertEqual(self.cfg.defaults.required_quality_checks, ["test", "lint", "build"])
        self.assertEqual(self.cfg.execution.model_dump(), before["execution"])
        self.assertNotIn("_kh_routing_resolved", self.cfg.model_dump())

    def test_legacy_validation_does_not_replace_resolved_per_run_choice_or_role_scope(self):
        self.profile([self.choice("recorded-builder")])
        override = routing.run_choices(self.cfg, ["builder=claude_code:run-only-builder:high"])
        selected, _ = routing.resolve(self.cfg, self.root, required_roles={"builder"},
                                       available=lambda _: True, overrides=override)
        next(a for a in selected.agents if a.name == "builder").writes = []
        available = Mock(return_value=True)
        routing.apply_legacy_choices(selected, self.root, ["builder"], available=available)
        available.assert_not_called()
        builder = next(a for a in selected.agents if a.name == "builder")
        self.assertEqual((builder.model, builder.writes), ("run-only-builder", []))
        self.assertEqual(next(a for a in self.cfg.agents if a.name == "builder").model, "base-builder")

    def test_unsupported_effort_rejects_before_availability_fallback(self):
        for changes in ({"thinking": "ultra"}, {"thinking": "high", "supported_thinking": ["low"]},
                        {"supported_thinking": ["medium", "ultra"]}):
            with self.subTest(changes=changes):
                self.profile([self.choice("unavailable", **changes), self.choice("valid")])
                available = Mock(return_value=True)
                with self.assertRaises(ValueError):
                    routing.resolve(self.cfg, self.root, available=available)
                available.assert_not_called()

    def test_invalid_unused_fallback_cannot_hide_behind_first_choice(self):
        self.profile([self.choice("valid"), self.choice("invalid", thinking="ultra")])
        available = Mock(return_value=True)
        with self.assertRaises(ValueError):
            routing.resolve(self.cfg, self.root, available=available)
        available.assert_not_called()

    def test_delegate_omission_preserves_tool_boundary_and_fails(self):
        self.profile([self.choice("codex-fixture", coding_agent="delegate_codex")])
        available = Mock(return_value=True)
        with self.assertRaisesRegex(ValueError, "tools: null"):
            routing.resolve(self.cfg, self.root, available=available)
        available.assert_not_called()

    def test_switch_to_delegate_requires_explicit_null_even_with_unrestricted_base(self):
        next(a for a in self.cfg.agents if a.name == "builder").tools = None
        self.profile([self.choice("codex-fixture", coding_agent="delegate_codex")])
        available = Mock(return_value=True)
        with self.assertRaises(ValueError):
            routing.resolve(self.cfg, self.root, available=available)
        available.assert_not_called()

    def test_explicit_delegate_null_is_accepted_without_broadening_writes(self):
        self.profile([self.choice("codex-fixture", coding_agent="delegate_codex", tools=None)])
        original = self.cfg.model_dump()
        resolved, _ = routing.resolve(self.cfg, self.root, available=lambda _: True)
        builder = next(a for a in resolved.agents if a.name == "builder")
        self.assertIsNone(builder.tools)
        self.assertEqual(builder.writes, ["src/**"])
        self.assertEqual(builder.thinking, "high")
        self.assertEqual(builder.supported_thinking, ["medium", "high"])
        self.assertEqual(self.cfg.model_dump(), original)

    def test_delegate_cannot_silently_drop_extensions(self):
        next(a for a in self.cfg.agents if a.name == "builder").harness_engineering = ["hook.ts"]
        self.profile([self.choice("codex-fixture", coding_agent="delegate_codex", tools=None)])
        with self.assertRaisesRegex(ValueError, "no extensions"):
            routing.resolve(self.cfg, self.root, available=lambda _: True)

    def test_profile_schema_rejects_permissions_and_gate_overrides(self):
        for extra in ({"writes": ["**"]}, {"harness_engineering": []},
                      {"protected_files": []}, {"quality_checks": []}):
            with self.subTest(extra=extra):
                self.profile([self.choice("model", **extra)])
                with self.assertRaises(ValueError):
                    routing.resolve(self.cfg, self.root, available=lambda _: True)
        for extra in ({"review_required": False}, {"required_quality_checks": []}):
            with self.subTest(extra=extra):
                self.profile([self.choice("model")], **extra)
                with self.assertRaises(ValueError):
                    routing.resolve(self.cfg, self.root, available=lambda _: True)

    def test_profile_checks_are_additive_and_result_is_detached(self):
        self.profile([self.choice("model")], required_checks=["build", "test"])
        before = copy.deepcopy(self.cfg.model_dump())
        resolved, decision = routing.resolve(self.cfg, self.root, available=lambda _: True)
        self.assertEqual(resolved.defaults.required_quality_checks, ["test", "lint", "build"])
        self.assertTrue(decision["review_required"])
        self.assertEqual(self.cfg.model_dump(), before)
        resolved.defaults.required_quality_checks.clear()
        resolved.agents[0].writes.append("owner-secret")
        self.assertEqual(self.cfg.model_dump(), before)

    def test_saved_profile_and_explicit_override_precedence(self):
        self.assertEqual(routing.choose_profile(self.cfg, self.root), "current")
        routing.save_preference(self.root, "alternate", self.cfg)
        self.assertEqual(routing.choose_profile(self.cfg, self.root), "alternate")
        _, decision = routing.resolve(self.cfg, self.root, "current", available=lambda _: True)
        self.assertEqual(decision["profile"], "current")
        self.assertEqual(routing.preference(self.root), "alternate")
        before = (self.root / ".kh/routing.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "Unknown routing profile"):
            routing.save_preference(self.root, "missing", self.cfg)
        self.assertEqual((self.root / ".kh/routing.json").read_bytes(), before)

    def test_resolution_and_inventory_are_read_only(self):
        with patch.object(routing, "binary_status", return_value={"fixture": "/fake/cli"}):
            inventory = routing.inventory(self.cfg, self.root)
        routing.resolve(self.cfg, self.root, available=lambda _: True)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn("does not establish authentication", inventory["notice"])
        for profile in inventory["profiles"].values():
            for choices in profile.values():
                for choice in choices:
                    self.assertEqual(choice["authentication"], "unverified")

    @unittest.skipUnless(os.name == "posix", "POSIX symlink-safe state implementation")
    def test_saved_preferences_refuse_symlink_file_and_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "routing.json"
        victim.write_text('{"profile":"current"}')
        state = self.root / ".kh"
        state.mkdir()
        link = state / "routing.json"
        link.symlink_to(victim)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            routing.save_preference(self.root, "alternate", self.cfg)
        self.assertEqual(victim.read_text(), '{"profile":"current"}')
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            routing.preference(self.root)
        link.unlink()
        state.rmdir()
        state.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            routing.save_preference(self.root, "alternate", self.cfg)
        self.assertEqual(victim.read_text(), '{"profile":"current"}')

    def test_malformed_saved_preference_is_not_silently_replaced(self):
        state = self.root / ".kh"
        state.mkdir()
        path = state / "routing.json"
        for data in ({"profile": "current", "extra": True}, {"profile": 4}, [],
                     {"profile": "no-such-profile"}):
            with self.subTest(data=data):
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    routing.choose_profile(self.cfg, self.root)


if __name__ == "__main__":
    unittest.main()
