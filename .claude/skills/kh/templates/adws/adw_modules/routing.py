"""Explicit project routing; installed executables are not authenticated models.

Fallback is considered only before any task dispatch, in the order configured
by the owner. Profiles cannot override write scopes, remove required checks
or reduce review sessions: a profile changes who thinks, not the rules.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import agent_cc, agent_delegate
from .data_types import AgentConfig, KHConfig, CodingAgent, RUNTIMES
from .scope import _atomic_json, _read_json

EFFORTS = {
    "claude_code": set(agent_cc.EFFORT_MAP),
    "delegate_codex": set(agent_delegate.CODEX_EFFORT_LEVELS),
}
RUNTIME_ALIASES = {}
LEGACY_ROLES = {"planner", "builder", "reviewer"}
EXPLICIT_ROLES = {"plan_reviewer", "project_architect"}
ROLES = LEGACY_ROLES | EXPLICIT_ROLES | {'documenter'}
REVIEWED_ROLES = LEGACY_ROLES | {"plan_reviewer"}
BOOTSTRAP_ROLES = {"project_architect", "plan_reviewer"}
WORKFLOW_ROLES = {
    "legacy": LEGACY_ROLES,
    "plan_review": {"planner", "plan_reviewer"},
    "bootstrap": BOOTSTRAP_ROLES,
    "start": REVIEWED_ROLES,
    "first_start": REVIEWED_ROLES | BOOTSTRAP_ROLES,
}


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coding_agent: CodingAgent
    model: str = Field(min_length=1)
    thinking: str
    supported_thinking: list[str] = Field(min_length=1)
    tools: list[str] | None = None

    @field_validator("model", "thinking")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("routing values cannot be blank")
        return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: dict[str, list[Choice]] = Field(default_factory=dict)
    required_checks: list[Literal["test", "lint", "typecheck", "build"]] = Field(default_factory=list)
    max_repairs: int = Field(default=2, ge=0, le=5)
    max_plan_repairs: int = Field(default=2, ge=0, le=5, strict=True)
    max_bootstrap_repairs: int = Field(default=2, ge=0, le=5, strict=True)

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value):
        if set(value) - ROLES or any(not choices for choices in value.values()):
            raise ValueError("profiles need nonempty choices for known routing roles")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_profile: str = "current"
    profiles: dict[str, Profile] = Field(default_factory=lambda: {"current": Profile()})


def settings(cfg):
    result = Settings.model_validate(cfg.routing or {})
    if result.default_profile not in result.profiles:
        raise ValueError("routing.default_profile is not defined")
    return result


def preference(root):
    path = Path(root) / ".kh/routing.json"
    if not path.exists() and not path.is_symlink(): return None
    data = _read_json(path)
    if not isinstance(data, dict) or set(data) != {"profile"} or not isinstance(data["profile"], str):
        raise ValueError("Malformed saved routing preference")
    return data["profile"]


def save_preference(root, name, cfg):
    if name not in _merged_settings(cfg, root).profiles: raise ValueError("Unknown routing profile")
    _atomic_json(Path(root) / ".kh/routing.json", {"profile": name})


def binary_status(runtime):
    names = list(RUNTIMES[runtime])
    if runtime == "claude_code": names = [os.environ.get("CLAUDE_CODE_PATH", "claude")]
    return {name: shutil.which(name) for name in names}


def installed(agent):
    return all(binary_status(agent.coding_agent).values())




def validate_effort(agent):
    if agent.thinking not in EFFORTS[agent.coding_agent]:
        raise ValueError(f"{agent.name}: unsupported {agent.coding_agent} effort {agent.thinking!r}")
    if agent.supported_thinking is not None:
        if not agent.supported_thinking or not set(agent.supported_thinking) <= EFFORTS[agent.coding_agent]:
            raise ValueError(f"{agent.name}: invalid supported_thinking declaration")
        if agent.thinking not in agent.supported_thinking:
            raise ValueError(f"{agent.name}: effort not in declared model capabilities")
    if agent.coding_agent == "delegate_codex" and (agent.tools is not None or agent.harness_engineering):
        raise ValueError(f"{agent.name}: Codex requires explicitly configured tools: null and no extensions")


class OwnerModels(BaseModel):
    """Per-role choices the owner recorded at project start (.kh/models.json).

    Written only by adw_setup_models.py after validation; read here as one
    additional routing profile layered over the YAML profiles. The owner file
    can narrow model/effort per role; it can never widen writes, drop checks
    or remove review — Profile fields are the whole schema.
    """
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    roles: dict[str, list[Choice]] = Field(default_factory=dict)
    required_checks: list[Literal["test", "lint", "typecheck", "build"]] = Field(default_factory=list)
    max_repairs: int = Field(default=2, ge=0, le=5)
    max_plan_repairs: int = Field(default=2, ge=0, le=5, strict=True)
    max_bootstrap_repairs: int = Field(default=2, ge=0, le=5, strict=True)
    note: str = ""
    recorded_at: str = ""

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value):
        if set(value) - ROLES or any(not choices for choices in value.values()):
            raise ValueError("owner models need nonempty choices for known routing roles")
        return value


def owner_models_path(root):
    return Path(root) / ".kh" / "models.json"


def reject_retired_choices(record, path):
    roles = record.get("roles", {}) if isinstance(record, dict) else {}
    for role, choices in (roles.items() if isinstance(roles, dict) else []):
        for index, choice in enumerate(choices if isinstance(choices, list) else []):
            if isinstance(choice, dict) and choice.get("coding_agent") in ("antigravity_cli", "gemini_cli", "pi"):
                raise ValueError(f"{path}: {role}[{index}] uses retired runtime {choice['coding_agent']}; "
                                 "run /kh-setup-models with an explicit supported --set for that role. "
                                 "Retired fallback entries must be explicitly removed or replaced in models.json first; "
                                 "no choices were converted or removed.")


def owner_profile(root):
    """The owner-recorded profile from project setup, or None when absent."""
    path = owner_models_path(root)
    if not path.exists() and not path.is_symlink(): return None
    raw = _read_json(path)
    reject_retired_choices(raw, path)
    parsed = OwnerModels.model_validate(raw)
    return parsed.name, Profile(roles=parsed.roles, required_checks=parsed.required_checks,
                                max_repairs=parsed.max_repairs, max_plan_repairs=parsed.max_plan_repairs,
                                max_bootstrap_repairs=parsed.max_bootstrap_repairs)


def _merged_settings(cfg, root):
    """YAML profiles plus the owner-recorded setup profile, if one exists."""
    result = settings(cfg)
    owner = owner_profile(root)
    if owner is not None:
        name, profile = owner
        if name in result.profiles:
            raise ValueError(f"owner setup profile name collides with a config profile: {name}")
        result.profiles[name] = profile
    return result


def choose_profile(cfg, root, requested=None):
    name = requested or preference(root) or settings(cfg).default_profile
    if name not in _merged_settings(cfg, root).profiles: raise ValueError(f"Unknown routing profile: {name}")
    return name


def workflow_readiness(cfg, profile):
    """Configuration readiness only: does not claim runtime or authentication readiness."""
    declared = {a.name for a in cfg.agents}
    explicit = EXPLICIT_ROLES | ({'documenter'} if cfg.execution.documentation.enabled else set())
    configured = declared - explicit | (declared & set(profile.roles))
    workflows = {name: set(roles) for name, roles in WORKFLOW_ROLES.items()}
    if cfg.execution.documentation.enabled:
        for name in ('start', 'first_start'):
            workflows[name].add('documenter')
    return {name: {"configured": not (roles - configured),
                   "missing_roles": sorted(roles - configured)}
            for name, roles in workflows.items()}


def canonical(runtime, model):
    """Record explicit names without runtime or model conversion."""
    return runtime, model


def parse_choice_spec(spec):
    """ROLE=RUNTIME:MODEL:EFFORT — model may contain '/' but not ':'."""
    role, sep, rest = spec.partition("=")
    if not sep: raise ValueError(f"expected ROLE=RUNTIME:MODEL:EFFORT, got {spec!r}")
    runtime, sep, rest = rest.partition(":")
    model, sep, effort = rest.rpartition(":")
    if not sep or not all(x.strip() for x in (role, runtime, model, effort)):
        raise ValueError(f"expected ROLE=RUNTIME:MODEL:EFFORT, got {spec!r}")
    return role.strip(), runtime.strip(), model.strip(), effort.strip()


def requested_choice(declared, role, runtime, model, effort):
    """One syntactic contract for owner setup and temporary role choices."""
    if role not in ROLES:
        raise ValueError(f"{role!r}: not a routing role; choose from {sorted(ROLES)}")
    if role not in declared:
        raise ValueError(f"{role!r}: no such agent in the config")
    if runtime not in RUNTIMES:
        raise ValueError(f"{role}: unknown runtime {runtime!r}; choose from {sorted([*RUNTIMES, *RUNTIME_ALIASES])}")
    if effort not in EFFORTS[runtime]:
        raise ValueError(f"{role}: effort {effort!r} not supported by {runtime}; choose from {sorted(EFFORTS[runtime])}")
    values = {"coding_agent": runtime, "model": model, "thinking": effort, "supported_thinking": [effort]}
    if runtime == "delegate_codex":
        values["tools"] = None  # An explicit switch keeps this CLI's native tools.
    return values


def configured_choice(base, choice):
    """Merge an explicit choice without silently widening the tool boundary."""
    if choice.coding_agent == 'delegate_codex' and base.coding_agent != 'delegate_codex' and (
        'tools' not in choice.model_fields_set or choice.tools is not None
    ):
        raise ValueError(f'{base.name}: switching to Codex requires explicit tools: null')
    return AgentConfig.model_validate({**base.model_dump(), **choice.model_dump(exclude_unset=True)})


def run_choices(cfg, specs):
    """The owner's model and effort per role for ONE run, as answered to the controller.

    Validated like /kh-setup-models (fail-closed on role, runtime and effort;
    a model name is taken as declared) but never written anywhere: the next run
    starts again from the recorded setup. The declared effort is the only
    capability claimed for the chosen model.
    """
    declared = {a.name: a for a in cfg.agents}
    problems, choices = [], {}
    for spec in specs or []:
        try:
            role, runtime, model, effort = parse_choice_spec(spec)
            runtime, model = canonical(runtime, model)
            if role in choices:
                raise ValueError(f"{role}: chosen twice for this run")
            values = requested_choice(declared, role, runtime, model, effort)
        except ValueError as error:
            problems.append(str(error)); continue
        choices[role] = Choice(**values)
    if problems:
        raise ValueError("invalid model choice for this run:\n- " + "\n- ".join(problems))
    return choices


def resolve(cfg: KHConfig, root, requested=None, available=installed, *, required_roles=None,
            overrides=None):
    """Return detached config and an auditable routing decision before dispatch.

    `overrides` ({role: Choice}, from run_choices) replaces the selected
    profile's choices for those roles in this run only.
    """
    name = choose_profile(cfg, root, requested)
    profile = _merged_settings(cfg, root).profiles[name]
    overrides = dict(overrides or {})
    if overrides:
        profile = profile.model_copy(update={"roles": {**profile.roles,
                                                        **{r: [c] for r, c in overrides.items()}}})
    required = LEGACY_ROLES if required_roles is None else set(required_roles)
    if not required or required - ROLES:
        raise ValueError("required_roles must contain known routing roles")
    declared = {a.name for a in cfg.agents}
    explicit = EXPLICIT_ROLES | ({'documenter'} if cfg.execution.documentation.enabled else set())
    missing = (required - declared) | (required & explicit - set(profile.roles))
    if missing:
        raise ValueError("Missing explicit model configuration for " + ", ".join(sorted(missing))
                         + "; run /kh-setup-models --interactive --apply")
    resolved = cfg.model_copy(deep=True)
    decisions = []
    for role in sorted(required):
        base = next((a for a in resolved.agents if a.name == role), None)
        if base is None: raise ValueError(f"Guarded startup requires agent {role}")
        alternatives = profile.roles.get(role)
        candidates = [configured_choice(base, choice) for choice in alternatives] if alternatives else [base]
        # Invalid configuration is never converted to a reason to expand fallback.
        for candidate in candidates: validate_effort(candidate)
        selected = next((a for a in candidates if available(a)), None)
        if selected is None: raise RuntimeError(f"No configured available runtime for {role}")
        index = candidates.index(selected)
        resolved.agents[resolved.agents.index(base)] = selected
        decisions.append({"role": role, "requested": _identity(candidates[0]),
                          "selected": _identity(selected), "fallback": index > 0,
                          "reason": "earlier configured CLI unavailable" if index else "configured first choice",
                          "authentication": "unverified", "effective_provider_setting": "unverified until execution"})
    resolved.defaults.required_quality_checks = list(dict.fromkeys(
        [*cfg.defaults.required_quality_checks, *profile.required_checks]))
    # Internal execution provenance only: never serialized into project config.
    # Legacy validation must not resolve this configuration again and replace
    # the guarded controller's explicit per-run choices or phase write scope.
    object.__setattr__(resolved, "_kh_routing_resolved", True)
    identity = hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()
    return resolved, {"profile": name, "config_sha256": identity, "roles": decisions,
                      "run_choices": {role: choice.model_dump(exclude_unset=True)
                                      for role, choice in sorted(overrides.items())},
                      "max_repairs": profile.max_repairs,
                      "max_plan_repairs": profile.max_plan_repairs,
                      "max_bootstrap_repairs": profile.max_bootstrap_repairs,
                      "required_checks": resolved.defaults.required_quality_checks,
                      "review_required": True}


def apply_legacy_choices(cfg: KHConfig, root, required, *, available=installed):
    """Apply persisted role choices before a legacy workflow validates its CLIs.

    Legacy entry points keep the loaded config and ignore validate's return, so
    update only their required roles and additive check names in place. Config
    loading itself stays raw, allowing model setup to repair invalid old records.
    """
    if getattr(cfg, "_kh_routing_resolved", False):
        return
    roles = set(required) & ROLES
    if not roles:
        return
    selected, _ = resolve(cfg, root, required_roles=roles, available=available)
    for index, agent in enumerate(cfg.agents):
        if agent.name in roles:
            cfg.agents[index] = next(a for a in selected.agents if a.name == agent.name)
    cfg.defaults.required_quality_checks = selected.defaults.required_quality_checks
    object.__setattr__(cfg, "_kh_routing_resolved", True)


def _identity(agent):
    return {"coding_agent": agent.coding_agent, "model": agent.model, "thinking": agent.thinking,
            "supported_thinking": agent.supported_thinking}


def inventory(cfg, root):
    config = _merged_settings(cfg, root)
    result = {"default_profile": choose_profile(cfg, root), "profiles": {},
              "workflow_readiness": {}, "repair_limits": {},
              "notice": "Configured model choices only. CLI presence does not establish authentication, model access or live effort support."}
    agents = {a.name: a for a in cfg.agents}
    for name, profile in config.profiles.items():
        roles = {}
        for role in sorted(ROLES):
            choices = profile.roles.get(role)
            items = [c.model_dump() for c in choices] if choices else (
                [_identity(agents[role])] if role in agents else [])
            for item in items:
                item["executables"] = binary_status(item["coding_agent"])
                item["authentication"] = "unverified"
            roles[role] = items
        result["profiles"][name] = roles
        result["workflow_readiness"][name] = workflow_readiness(cfg, profile)
        result["repair_limits"][name] = {key: getattr(profile, key) for key in
            ("max_repairs", "max_plan_repairs", "max_bootstrap_repairs")}
    return result


def choice_menu(cfg, root, requested=None, roles=None):
    """What the controller offers the owner per role before a run (read-only).

    `current` is what this run would use without an answer. `options` lists it
    first, then other choices already configured somewhere in this project
    (profiles, fallbacks, agent defaults), then the current model at other
    efforts. Nothing outside the project's own configuration is offered: a new
    model is typed by the owner as RUNTIME:MODEL:EFFORT.
    """
    name = choose_profile(cfg, root, requested)
    merged = _merged_settings(cfg, root)
    profile = merged.profiles[name]
    agents = {a.name: a for a in cfg.agents}
    wanted = sorted(roles or ROLES)

    def key(item):
        return (item["coding_agent"], item["model"], item["thinking"])

    menu = {}
    for role in wanted:
        if role not in agents:
            continue
        chosen = profile.roles.get(role)
        current = ({k: chosen[0].model_dump()[k] for k in ("coding_agent", "model", "thinking")}
                   if chosen else (None if role in EXPLICIT_ROLES else
                                   {k: getattr(agents[role], k) for k in ("coding_agent", "model", "thinking")}))
        seen, options = set(), []
        def add(item):
            if item and key(item) not in seen:
                seen.add(key(item)); options.append(dict(item))
        add(current)
        for other in merged.profiles.values():
            for choice in other.roles.get(role, []):
                add({k: getattr(choice, k) for k in ("coding_agent", "model", "thinking")})
        if role not in EXPLICIT_ROLES:
            add({k: getattr(agents[role], k) for k in ("coding_agent", "model", "thinking")})
        if current:
            for effort in ("medium", "high", "xhigh", "low"):
                if effort in EFFORTS[current["coding_agent"]]:
                    add({**current, "thinking": effort})
        menu[role] = {"current": current, "options": options[:4],
                      "answer_format": "RUNTIME:MODEL:EFFORT"}
    return {"profile": name, "roles": menu, "runtimes": sorted([*RUNTIMES, *RUNTIME_ALIASES]),
            "efforts": {r: sorted(e) for r, e in EFFORTS.items()},
            "notice": "Configured choices only; a typed model is recorded as declared and checked "
                      "at preflight. Answers apply to this run only."}
