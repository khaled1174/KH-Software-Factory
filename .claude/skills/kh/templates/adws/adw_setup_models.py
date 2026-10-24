#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Owner-driven model/effort setup, recorded once at project start.

The owner — not this package — decides which model and reasoning effort each
workflow role (project_architect, planner, plan_reviewer, builder, reviewer, documenter) runs on. This script records
those explicit choices as one routing profile in `.kh/models.json` and, with
--apply, selects it for the project. Validation is syntactic and fail-closed:
unknown role, unknown runtime or an effort the runtime does not support are
errors. A model name is recorded as declared — checked only as a non-blank
string — so each name must identify a model actually available in the chosen
runtime. No provider call is made; CLI presence and authentication stay
unverified until the guarded startup's own preflight.

Default persistence refuses an active writer lease. Wait until the current run
ends before recording choices for a later run; no provider is launched.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import sys

from adw_modules import agents, routing
from adw_modules.scope import _atomic_json, _parent_fd, _read_json
from pydantic import Field


# Friendly CLI names are normalized by routing.RUNTIME_ALIASES.
RUNTIME_ALIASES = routing.RUNTIME_ALIASES
canonical = routing.canonical


parse_set = routing.parse_choice_spec


def parse_supported(spec):
    role, sep, efforts = spec.partition("=")
    if not sep: raise ValueError(f"--supported expects ROLE=effort,effort, got {spec!r}")
    values = [e.strip() for e in efforts.split(",") if e.strip()]
    if not values: raise ValueError(f"--supported for {role!r} is empty")
    return role.strip(), values




def collect_interactive(cfg, base):
    """Prompt per configured routing role; empty answers keep the current value."""
    choices = {}
    for role in sorted(routing.ROLES):
        if role == 'documenter' and not cfg.execution.documentation.enabled and role not in base:
            continue
        agent = next((a for a in cfg.agents if a.name == role), None)
        if agent is None: continue
        current = base.get(role) or {}
        shown = {"coding_agent": current.get("coding_agent", agent.coding_agent),
                 "model": current.get("model", agent.model),
                 "thinking": current.get("thinking", agent.thinking)}
        print(f"{role}: current {shown['coding_agent']} / {shown['model']} / {shown['thinking']}")
        names = sorted([*routing.RUNTIMES, *RUNTIME_ALIASES])
        runtime = input(f"  runtime {names} [{shown['coding_agent']}]: ").strip() or shown["coding_agent"]
        model = input(f"  model [{shown['model']}]: ").strip() or shown["model"]
        effort_runtime = RUNTIME_ALIASES.get(runtime.casefold(), (runtime,))[0]
        effort = input(f"  effort {sorted(routing.EFFORTS.get(effort_runtime, ()))} [{shown['thinking']}]: ").strip() or shown["thinking"]
        choices[role] = (runtime, model, effort)
    return choices


class RetiredChoice(routing.Choice):
    """Read historical data for explicit owner repair, never for dispatch."""
    coding_agent: Literal["antigravity_cli", "gemini_cli", "pi"]


class SetupOwnerModels(routing.OwnerModels):
    roles: dict[str, list[routing.Choice | RetiredChoice]] = Field(default_factory=dict)


def load_owner_record(root):
    """Keep explicit nulls distinct from omitted fields when loading owner choices."""
    path = routing.owner_models_path(root)
    if not path.exists() and not path.is_symlink():
        return {}
    return SetupOwnerModels.model_validate(_read_json(path)).model_dump(exclude_unset=True)


def build_choices(cfg, specs, supported_specs, interactive=False, existing_record=None):
    """Validate every requested choice; return {role: choice-dict}. Fail-closed."""
    existing = load_owner_record(Path.cwd()) if existing_record is None else existing_record
    base = {role: options[0] for role, options in existing.get("roles", {}).items()}
    requested = {}
    if interactive:
        requested = collect_interactive(cfg, base)
    for spec in specs or []:
        role, runtime, model, effort = parse_set(spec)
        requested[role] = (runtime, model, effort)
    supported = {}
    for spec in supported_specs or []:
        role, values = parse_supported(spec)
        if role in supported:
            raise ValueError(f"duplicate --supported declaration for {role!r}")
        supported[role] = values
    problems, choices = [], {}
    for role in supported.keys() - requested.keys():
        problems.append(f"--supported {role!r} requires a matching --set (or interactive choice)")
    declared = {a.name: a for a in cfg.agents}
    for role, (runtime, model, effort) in requested.items():
        try:
            runtime, model = canonical(runtime, model)
            routing.requested_choice(declared, role, runtime, model, effort)
        except ValueError as error:
            hint = " (scout keeps its config-level settings)" if role not in routing.ROLES else ""
            problems.append(str(error) + hint); continue
        agent = declared[role]
        current = deepcopy(base.get(role, {}))
        caps = supported.get(role, current.get("supported_thinking", agent.supported_thinking))
        if caps is not None:
            if not caps or not set(caps) <= routing.EFFORTS[runtime]:
                problems.append(f"{role}: supported_thinking {caps} exceeds {runtime} efforts"); continue
            if effort not in caps:
                problems.append(f"{role}: effort {effort!r} not in declared capabilities {caps}; "
                                "widen them explicitly with --supported if the model truly supports it")
                continue
        choice = {**current, "coding_agent": runtime, "model": model, "thinking": effort,
                  "supported_thinking": list(caps) if caps is not None else sorted(routing.EFFORTS[runtime])}
        if runtime == "delegate_codex":
            # An explicit runtime switch retains the established CLI contract:
            # use native capabilities, leaving source write scope intact.
            # routing.resolve distinguishes omitted tools from this explicit null.
            choice["tools"] = None
            if agent.harness_engineering:
                problems.append(f"{role}: Codex cannot carry harness extensions; "
                                "clear harness_engineering in the config first"); continue
        choices[role] = choice
    if problems:
        raise ValueError("invalid model setup:\n- " + "\n- ".join(problems))
    return choices


def validate_record(cfg, record):
    """Apply identical configuration checks before preview and persistence."""
    routing.reject_retired_choices(record, routing.owner_models_path(Path.cwd()))
    parsed = routing.OwnerModels.model_validate(record)
    if not parsed.name.strip():
        raise ValueError("profile name cannot be blank")
    if parsed.name in routing.settings(cfg).profiles:
        raise ValueError(f"profile name {parsed.name!r} already exists in the config; choose another")
    declared = {agent.name: agent for agent in cfg.agents}
    for role, alternatives in parsed.roles.items():
        base = declared.get(role)
        if base is None:
            raise ValueError(f"{role!r}: no such agent in the config")
        for choice in alternatives:
            routing.validate_effort(routing.configured_choice(base, choice))
    # Roles that inherit their settings from YAML must also be usable.
    for role in routing.ROLES - parsed.roles.keys():
        if role in declared:
            routing.validate_effort(declared[role])


def _record_digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def persist_record(root, record, cfg, *, expected_digest=None):
    """Default choices change only while the repository has no active writer."""
    from adw_modules.session import acquire_repo_lock
    validate_record(cfg, record)
    try:
        lease = acquire_repo_lock(root)
    except RuntimeError as error:
        if "BUSY" in str(error):
            raise RuntimeError("A KH run is active: project defaults cannot change during execution. "
                               "Wait until the active run ends, then record choices for a later run.") from error
        raise
    with lease:
        _persist_record_unlocked(root, record, cfg, expected_digest=expected_digest)


def _persist_record_unlocked(root, record, cfg, *, expected_digest=None):
    """Use atomic writes and restore both records if persistence raises."""
    validate_record(cfg, record)
    path = routing.owner_models_path(root)
    previous = load_owner_record(root)
    if expected_digest is not None and _record_digest(previous) != expected_digest:
        raise RuntimeError("STALE_MODEL_SETUP: owner model choices changed after this preview. "
                           "Rerun setup to merge the latest roles; no choices were written.")
    # Reject malformed/non-regular saved preferences before changing models.
    old_preference = routing.preference(root)
    try:
        _atomic_json(path, record)
        routing.save_preference(root, record["name"], cfg)
    except Exception as original:
        rollback_errors = []
        for target, value in ((path, previous or None),
                              (Path(root) / ".kh/routing.json",
                               {"profile": old_preference} if old_preference is not None else None)):
            try:
                if value is not None:
                    _atomic_json(target, value)
                elif target.exists() or target.is_symlink():
                    with _parent_fd(target) as (parent, name):
                        os.unlink(name, dir_fd=parent)
                        os.fsync(parent)
            except Exception as error:
                rollback_errors.append(str(error))
        if rollback_errors:
            raise RuntimeError(f"Model setup failed ({original}); rollback also failed: "
                               + "; ".join(rollback_errors)) from original
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("KH_CONFIG", "adws/adw_kh_config/kh.config.yaml"))
    parser.add_argument("--set", action="append", metavar="ROLE=RUNTIME:MODEL:EFFORT",
                        help="Owner choice for one role; repeatable.")
    parser.add_argument("--supported", action="append", metavar="ROLE=effort,effort",
                        help="Declared effort capabilities for the role's model; repeatable.")
    parser.add_argument("--profile", help="Name of the owner profile. An explicit new name renames the "
                        "existing profile while preserving its settings; omitted keeps its name (initially owner).")
    parser.add_argument("--interactive", action="store_true", help="Prompt per role, showing current values.")
    parser.add_argument("--note", help="Replace the recorded reason; omitted preserves the existing note.")
    parser.add_argument("--apply", action="store_true",
                        help="Persist default choices while idle; never launch a model. Default is a dry-run preview.")
    args = parser.parse_args()
    if not args.set and not args.interactive:
        parser.error("nothing to record: pass --set ROLE=RUNTIME:MODEL:EFFORT (repeatable) or --interactive")
    root = Path.cwd()
    cfg = agents.load_config(args.config)
    existing = load_owner_record(root)
    choices = build_choices(cfg, args.set, args.supported, args.interactive, existing)
    record = {"required_checks": [], "max_repairs": 2, "max_plan_repairs": 2,
              "max_bootstrap_repairs": 2, "note": "", **deepcopy(existing)}
    record["name"] = args.profile if args.profile is not None else existing.get("name", "owner")
    roles = record.setdefault("roles", {})
    for role, choice in choices.items():
        # Updating one primary model never removes its owner's fallback choices.
        roles[role] = [choice, *roles.get(role, [])[1:]]
    if args.note is not None:
        record["note"] = args.note
    record["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    validate_record(cfg, record)
    routing.preference(root)
    preview = {"profile": record["name"], "applied": bool(args.apply), "roles": record["roles"],
               "required_checks": record["required_checks"], "max_repairs": record["max_repairs"],
               "max_plan_repairs": record["max_plan_repairs"],
               "max_bootstrap_repairs": record["max_bootstrap_repairs"],
               "workflow_readiness": routing.workflow_readiness(cfg, routing.Profile.model_validate(
                   {key: value for key, value in record.items() if key in routing.Profile.model_fields})),
               "note": record["note"],
               "effects": {"provider_processes": 0, "network_calls": 0,
                           "paths": [".kh/models.json", ".kh/routing.json"],
                           "coordination_paths": [".kh/controller.lock"] if args.apply else [],
                           "written_paths": [".kh/models.json", ".kh/routing.json"] if args.apply else []},
               "notice": "Owner-recorded choices only. CLI presence, authentication, model access "
                         "and live effort support remain unverified until guarded-startup preflight."}
    if args.apply:
        persist_record(root, record, cfg, expected_digest=_record_digest(existing))
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if not args.apply:
        print("dry-run: nothing written; re-run with --apply to record and select this profile")
        return 0
    print(f"recorded {routing.owner_models_path(root)} and selected profile {record['name']!r} for this project")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(f"kh-setup-models: {error}", file=sys.stderr)
        raise SystemExit(1)
