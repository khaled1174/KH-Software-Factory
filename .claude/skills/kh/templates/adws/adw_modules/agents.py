"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

import yaml

from . import agent_cc, context_guard, permissions, prompts, transient, utils, workspace, gates

DIFF_INLINE_CHARS = 60_000   # larger controller diffs are read from the evidence file
from .data_types import (AgentCall, AgentConfig, CCRequest, EnvelopeBase, EventRecord, ExecutionSettings,
                         GateCheck, GateReport, Phase, PiResult, KHConfig, _valid_cost,
                         UsageBreakdown, RUNTIMES)
from .utils import new_id
from .process_io import RoleCleanupError, RoleTimeout

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON


# Corrections a wrong changed-file claim may use beyond the phase's retries.
REPORT_ONLY_CORRECTIONS = 2


class GateFailure(RuntimeError):
    pass


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "adws/adw_kh_config/kh.config.yaml") -> KHConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in ("coding_agent", "model", "thinking", "color", "tools", "writes"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
    cfg = KHConfig(**raw)
    # Apply the project's .env boundary before any agent or check can be
    # dispatched. Withheld names are printed once: a credential that is
    # deliberately unreachable must be distinguishable from a provider outage.
    withheld = utils.set_dotenv_allowlist(cfg.defaults.env_allowlist)
    if withheld:
        print(f"env_allowlist: withholding {len(withheld)} .env variable(s) from agents "
              f"and quality commands: {', '.join(withheld)}")
    return cfg


def resolve(cfg: KHConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")




def _runtime_instructions(run, agent, output_type):
    """Render the actual transport's boundary anew for every provider call."""
    if output_type.__name__ == 'DocumentationProposalOutput':
        return ('GUARDED DOCUMENTATION PROPOSAL: the current source is read-only. '
                'Standalone document-copy instructions do not apply. Return document contents inline '
                'using the supplied output schema. Only the controller may publish exact approved paths; '
                'write evidence only in the current phase directory. Never alter instructions or permissions.')
    if output_type.__name__ == 'BuildOutput':
        from . import command_limits
        return command_limits.instruction(agent.timeout_seconds)
    return ''


def _inside_git_work_tree() -> bool:
    import subprocess
    try:
        probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True,
                               timeout=10, check=False, env=utils.operator_env())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def validate(cfg: KHConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent."""
    from . import routing
    try:
        routing.apply_legacy_choices(cfg, Path.cwd(), required)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(f"config validation failed: {error}") from error
    problems = []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        if agent.coding_agent not in RUNTIMES:
            problems.append(f"agent {name!r}: unknown coding_agent {agent.coding_agent!r}")
            continue
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        if agent.coding_agent == "delegate_codex":
            import shutil
            if not shutil.which("node"):
                problems.append(f"agent {name!r}: node is required for the relay")
            if agent.tools is not None or agent.harness_engineering:
                problems.append(f"agent {name!r}: delegate requires tools: null and no extensions")
            if not _inside_git_work_tree():
                problems.append(f"agent {name!r}: Codex runs only inside a git repository (it refuses with "
                                "\"Not inside a trusted directory\"); run `git init` in the project once")
        else:
            problems.extend(f"agent {name!r}: {p}" for p in agent_cc.preflight())
            try:
                agent_cc.resolve_model(agent.model)
            except ValueError as e:
                problems.append(f"agent {name!r}: {e}")
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def _deliver_phase_records(run, phase: Phase, agent, isolated, status: str) -> None:
    """Export the phase's command records into the task handoff for every later role (SYS-02)."""
    from . import evidence_packet
    ledger = vars(isolated.view).get("command_ledger")
    evidence_packet.export_commands(run, phase.phase_id, agent.name, ledger,
                                    getattr(run, "owner_decisions", None), status=status,
                                    native_dir=Path(run.session_dir) / agent.name / "phases" / phase.phase_id)
    history = vars(isolated.view).get("gate_history")
    if history is not None:
        store = getattr(run, "gate_history_by_phase", None)
        if store is None:
            store = {}
            run.gate_history_by_phase = store
        store.pop(phase.phase_id, None)      # the latest phase is always last
        store[phase.phase_id] = gate_summary(history)


def gate_summary(history) -> list[dict]:
    """Attempt-level gate results: a later pass never hides an earlier failure (SYS-07)."""
    by_gate: dict[str, list] = {}
    for row in history or []:
        by_gate.setdefault(row["gate"], []).append(row)
    summary = []
    for name, rows in by_gate.items():
        failed = [r["attempt"] for r in rows if not r["ok"]]
        final = rows[-1]
        summary.append({"gate": name, "attempts": len(rows), "failed_attempts": failed,
                        "final": "passed" if final["ok"] else "failed",
                        "text": (f"{name}: failed attempt {', '.join(map(str, failed))}; "
                                 f"{'passed' if final['ok'] else 'failed'} attempt {final['attempt']}")
                                if failed else f"{name}: passed attempt {final['attempt']}",
                        "violations": {r["attempt"]: r["violations"] for r in rows if r["violations"]}})
    return summary


def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    workspace.require_role_cleanup(run)
    agent = resolve(run.cfg, phase.params.owner)
    with workspace.AgentWorkspace(run, phase, agent) as isolated:
        mapped_call = isolated.remap_input(call)
        try:
            result = _execute(isolated.view, phase, mapped_call)
        except BaseException as error:
            pending = workspace.remember_role_cleanup(isolated.view, error,
                phase_id=phase.phase_id, agent=agent.name)
            if pending:
                run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id, type='error',
                    name='role_cleanup_pending', payload={**pending, 'workspace': str(isolated.root),
                        'error': str(error)[:500]}))
            try:
                _deliver_phase_records(run, phase, agent, isolated, "failed")
            except Exception as export_error:  # never mask the real failure
                run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id, type="log",
                                             name="command_records_not_delivered",
                                             payload={"error": str(export_error)[:500]}))
            # A builder that stopped mid-work (timeout, provider failure, or the
            # controller itself being stopped: SIGTERM/SIGINT arrive as SystemExit) keeps
            # what it wrote: imported into the task's staging copy only, under
            # the same permission checks as a promotion, for the next run.
            if not pending and call.output_type.__name__ == "BuildOutput" and getattr(run, "salvage_failed_build", False):
                try:
                    kept = isolated.salvage()
                except Exception as salvage_error:  # never mask the real failure
                    kept, note = None, str(salvage_error)
                else:
                    note = "" if kept is not None else "not kept: permission or owner-change check failed"
                if isinstance(error, RoleTimeout):
                    error.resume_safe = kept is not None
                    error.salvaged_paths = kept or []
                rejected = getattr(isolated, "rejected", None) or []
                if rejected:
                    from .resume import baseline_identity
                    store = getattr(run, "rejected_paths", None)
                    if store is None:
                        store = []
                        run.rejected_paths = store
                    for item in rejected:
                        store.append({**item, "phase": phase.phase_id,
                                      "baseline": baseline_identity(getattr(run, "task_baseline", None), item["path"])})
                run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id, type="log",
                                             name="build_salvaged", payload={"paths": kept or [], "note": note,
                                                                             "rejected": rejected,
                                                                             "error": str(error)[:500]}))
            raise
    _deliver_phase_records(run, phase, agent, isolated, "completed")
    if hasattr(run, "record_observations"):
        run.record_observations(result, phase)
    if type(result).__name__ != "ReviewedPlanOutput" and hasattr(result, "acceptance_criteria") and result.acceptance_criteria:
        from .scope import add_plan_requirements
        add_plan_requirements(run, result.acceptance_criteria)
    return result


def _controller_diff(run):
    """Repo-relative controller diff resolved inside the current workspace."""
    relative = getattr(run, "controller_diff", None)
    return Path(run.repo_root) / relative if relative else None


def _normalize_cost(result, spent, run):
    """Keep adapter, phase and session money finite and consistently reported."""
    for field in type(result.usage).model_fields:
        if field.endswith('_cost') and not _valid_cost(getattr(result.usage, field)):
            setattr(result.usage, field, 0.0)
            result.cost_known = False
    if not _valid_cost(result.cost):
        result.cost = result.usage.total_cost
        result.cost_known = False
    # The adapter total is authoritative; some transports lack a breakdown.
    result.usage.total_cost = result.cost
    for field in type(result.usage).model_fields:
        if field.endswith('_cost') and not _valid_cost(
                getattr(spent, field) + getattr(result.usage, field)):
            setattr(result.usage, field, 0.0)
            result.cost_known = False
    result.cost = result.usage.total_cost
    if not _valid_cost(getattr(run, 'cost', 0.0) + result.cost):
        result.cost = result.usage.total_cost = 0.0
        result.cost_known = False


def _execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> provider -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    agent_dir = run.session_dir / agent.name
    invocation_dir = agent_dir / "phases" / phase.phase_id
    invocation_dir.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)

    from . import command_ledger, dispositions, evidence_packet, infrastructure
    breaches: list[dict] = []
    policy_settings = run.cfg.execution.command_policy
    ledger = None
    if policy_settings.enabled:
        ledger = command_ledger.CommandLedger(
            Path(run.session_dir) / "commands" / phase.phase_id, phase_id=phase.phase_id, agent=agent.name,
            runtime=agent.coding_agent, cwd=str(run.repo_root), classes=policy_settings.classes,
            forbidden=infrastructure.forbidden_patterns(), on_forbidden=breaches.append,
            inspect=infrastructure.inspect, exec_dir=Path(run.context_handoff_dir) / "exec")
    infrastructure.set_phase_env({"KH_EVIDENCE_DIR": run.context_handoff_dir})
    run.command_ledger = ledger
    try:
        return _execute_inner(run, phase, call, agent, agent_dir, invocation_dir, ledger, breaches)
    except BaseException as error:
        workspace.remember_role_cleanup(run, error, phase_id=phase.phase_id, agent=agent.name)
        raise
    finally:
        if ledger is not None:
            ledger.finalize()


def _execute_inner(run, phase, call, agent, agent_dir, invocation_dir, ledger, breaches):
    from . import command_ledger, dispositions, evidence_packet, infrastructure
    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "plan_envelope": call.plan.model_dump_json(indent=2) if call.plan else "(none)",
        "controller_diff": str(_controller_diff(run) or "(none)"),
        "context_handoff_dir": str(run.context_handoff_dir),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables,
                              stop_at='\n## Standalone DocumentOutput\n'
                              if call.output_type.__name__ == 'DocumentationProposalOutput' else None)
    user_text += '\nConfigured task kind: ' + run.cfg.execution.task_kind
    prompts.save(invocation_dir / "prompts", "system.md", system_text)
    prompts.save(invocation_dir / "prompts", "user.md", user_text)

    # Controller-owned requirements and exact output schema accompany every call.
    system_text += "\n\nWrite evidence only under " + str(run.context_handoff_dir)
    system_text += "\nDo not modify .git, control files, other phases, or paths outside this workspace."
    system_text += "\nRequired output JSON schema: " + json.dumps(call.output_type.model_json_schema())
    contract = getattr(run, "scope_contract", None)
    if contract is not None:
        user_text += "\nController task scope (cannot be changed by a worker): " + contract.model_dump_json()
        user_text += ("\nPut plans and review evidence only in the supplied current-phase evidence directory. "
                      "Keep source changes within the task and permitted paths. Report unrelated improvements as observations.")
        if call.output_type.__name__ == 'ReviewOutput':
            user_text += ("\nIf a code-change blocker needs a concrete file omitted from the approved plan, "
                          "name it in repair_paths and explain that requirement in blocking. The controller "
                          "will obtain a revised plan and independent approval before authorizing any write. "
                          "repair_paths never grants permission or changes the owner's task scope.")
    policy = getattr(run, "execution_policy", None)
    if policy is not None:
        user_text += "\nController execution limits: " + policy.model_dump_json()
        if policy.mode == 'light':
            user_text += ("\nLight workflow: keep output concise and actionable. Preserve every requirement, "
                          "applicable rule, allowed path and required check. Report security, migration, "
                          "destructive or architecture-wide work as REQUIRES_FULL_MODE; do not expand scope.")
    context_packet = getattr(run, "project_context_packet", None)
    if context_packet is not None:
        packet_text = json.dumps(context_packet, ensure_ascii=False)
        # A planning brief already carries the packet verbatim; a second copy
        # only doubles the prompt (and once pushed it past the argv limit).
        if packet_text not in user_text:
            user_text += "\nController project context (applicable documents and digests): " + packet_text
    if call.output_type.__name__ == "ReviewOutput" and getattr(run, "verification_result", None):
        user_text += "\nController verification evidence for this candidate (inspect applicable captures/results as well as source): " + json.dumps(run.verification_result, ensure_ascii=False)
    if call.output_type.__name__ == 'ReviewOutput' and getattr(run, 'documentation_result', None):
        user_text += '\nController documentation receipt (verify content, scope and current vs historical status): ' + json.dumps(run.documentation_result, ensure_ascii=False)
    review_paths = getattr(run, "scope_review_paths", None)
    if call.output_type.__name__ == "ReviewOutput" and review_paths is not None:
        user_text += ("\nIndependently check the full task delta, including non-goals. Return one scope_findings "
                      "entry per changed source path with path, within_scope, and concrete reason: " + json.dumps(review_paths))
        diff_path = _controller_diff(run)
        if diff_path:
            user_text += ("\nController-measured unified diff of the whole task (authoritative; the builder's "
                          "changed_files is a claim, this is the measurement): " + str(diff_path))
            try:
                diff_text = Path(diff_path).read_text(encoding="utf-8")
                if len(diff_text) <= DIFF_INLINE_CHARS:
                    user_text += "\n```diff\n" + diff_text + "```"
                else:
                    user_text += f"\n(diff is {len(diff_text)} chars; read the file)"
            except OSError:
                pass
        verification = getattr(run, "verification_paths", None)
        if verification:
            user_text += ("\nThese changed files define or configure verification. Approval requires a finding "
                          "with evidence that each is justified by the task and does not weaken, skip or delete "
                          "existing checks: " + json.dumps(verification))
    if getattr(run, "required_requirements", None):
        user_text += "\n\nController acceptance requirements: " + json.dumps(run.required_requirements)
    if call.output_type.__name__ in {"PlanOutput", "ReviewedPlanOutput"}:
        user_text += "\nReturn nonempty acceptance_criteria derived from the request and a nonempty plan artifact."
    if call.output_type.__name__ == "ReviewOutput":
        user_text += "\nReturn a finding with concrete evidence for every controller requirement, plus a review artifact."
    if call.output_type.__name__ == "BuildOutput" and getattr(run, "salvage_failed_build", False):
        from .resume import PROGRESS_INSTRUCTION
        user_text += "\n" + PROGRESS_INSTRUCTION.format(path=Path(run.context_handoff_dir) / "progress.json")
    if call.output_type.__name__ == "BuildOutput":
        # The builder picks the timeout of every command it starts; stating the
        # limit here keeps one hung test from using the rest of the role's time.
        user_text += (
            "\nchanged_files is THIS phase's change set only (the controller checks it exactly). "
            "Optionally report cumulative_task_changed_files: the whole task's change set including resumed "
            "work and earlier attempts; it is checked against the controller's task delta, never mixed up "
            "with changed_files.")
    # Controller-delivered records (SYS-02/03/09) and evidence conventions (SYS-07).
    user_text += evidence_packet.plan_instruction(run) + evidence_packet.commands_instruction(run)
    policy_record = getattr(run, "evidence_policy", None)
    if policy_record is not None:
        policy_text = dispositions.instruction(
            policy_record, str(evidence_packet.inputs_dir(run) / "policy.json"))
        user_text += policy_text
        if policy_record["digest"] not in user_text:
            raise RuntimeError("EVIDENCE_POLICY_NOT_RENDERED: the prompt lacks the effective policy digest")
    if ledger is not None:
        user_text += (
            "\nEvery shell command you run is recorded by the controller (exact text, timing, outcome and "
            "the full output you receive), whatever tool or wrapper you use. Commands are classified from that "
            "record; you cannot relabel one. Strict classes (tests, formatting, lint, probes) need an exit "
            "status the shell actually reports for that command and unfiltered output: no `| tail`, `| grep` "
            "or `; echo $?` around them; use `set -o pipefail` if you must pipe. Exploratory compilation may be "
            "filtered; its record is then an excerpt. Preferred route for every strict command: "
            "`python3 adws/kh_exec.py --class test --timeout SECONDS -- <program> <args>` runs one program without "
            "a shell, keeps its complete stdout/stderr and true exit/signal/timeout under your evidence directory "
            "and shows a shortened display; run each strict command on its own (one exit status cannot stand for "
            "two commands).")
    user_text += (
        "\nArtifacts: list absolute paths inside your evidence directory. A relative path resolves against "
        "the workspace root, not your evidence directory. Optionally describe each in artifact_declarations "
        "{path, kind: report|raw_stream|data|diff|other, base: evidence|repo, invocation_id, sha256, bytes}; "
        "reports must have content, and an empty raw stream is accepted only with the invocation_id of the "
        "controller command record that produced it.")
    if call.output_type.__name__ == "ReviewOutput":
        user_text += (
            "\nStart each blocking item with a stable finding ID (for example `B3: ...`). Reuse the ID of an "
            "earlier finding it continues (a revision), and give a distinct new problem a new ID (for example "
            "`B3-D2: ...`). A carried earlier finding appears as a requirement that names its ID; your finding for "
            "it may restate the text but must keep the requirement's opening words up to and including that ID."
            "\nReturn blocker_categories: one per blocking item, same order. code_change: the source must change. "
            "evidence_delivery: authentic records that exist were not available to you (name them). "
            "historical_evidence_unavailable: the required original record never existed or is lost; no rebuild "
            "can recreate it. owner_decision: only the owner can decide. infrastructure_or_provider: an "
            "environment or provider problem. Classify by what the requirement needs, checked against the "
            "controller record inventory, not by wording; a mixed problem lists code_change.")
    prompts.save(invocation_dir / "prompts", "system.md", system_text)
    prompts.save(invocation_dir / "prompts", "user.md", user_text)
    run.active_phase_id = phase.phase_id
    session_id = _agent_session_id(run, agent)
    send_number = 0
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": session_id,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.model, session_id)

    # Parse retries and gate corrections re-enter the SAME coding-agent session,
    # so the last send is the one whose context occupancy is current — while
    # spend is the opposite: every send costs, so usage accumulates across all
    # of them.
    latest: PiResult | None = None
    spent = UsageBreakdown()
    cost_known = True

    if agent.harness_engineering and agent.coding_agent == "claude_code":
        # Legacy TS extensions are not loadable by claude — MCP servers and hooks
        # live in Claude Code's own settings. Reported, never silently dropped.
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="extensions_skipped",
                                     payload={"agent": agent.name,
                                              "harness_engineering": agent.harness_engineering}))

    from . import execution_budget
    compatibility_report = None

    def prepare_send(prompt_text):
        nonlocal compatibility_report
        from . import runtime_compatibility
        required_tests = (getattr(call.plan, 'required_test_commands', [])
                          if call.output_type.__name__ == 'BuildOutput' else [])
        compatibility_report = runtime_compatibility.check(run, agent, required_test_commands=required_tests)
        return prompt_text

    def raw_send(prompt_text: str, retry: bool = False) -> PiResult:
        nonlocal latest, send_number, cost_known
        # Delivered controller records must be intact before any model sees this workspace.
        evidence_packet.verify(run, invocation_dir / "delivery_receipt.json")
        if not retry:
            # A provider-outage retry repeats a send already charged here.
            execution_budget.reserve_model_send(run, 'substantive' if send_number == 0 else 'correction')
        timeout = execution_budget.remaining_timeout(run, agent.timeout_seconds)
        send_number += 1
        attempt_dir = invocation_dir / f"attempt-{send_number:03d}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        runtime_text = _runtime_instructions(run, agent, call.output_type)
        dispatch_system = system_text + '\n\n' + runtime_text
        prompt_text += '\n\nCurrent runtime boundary:\n' + runtime_text
        prompts.save(attempt_dir, "system.md", dispatch_system)
        prompts.save(attempt_dir, "user.md", prompt_text)
        selection = {'coding_agent': agent.coding_agent, 'model': agent.model, 'thinking': agent.thinking,
                     'session_id': session_id, 'attempt': send_number}
        prompts.save(attempt_dir, 'model_choice.json', json.dumps(selection, ensure_ascii=False, indent=2))
        prompts.save(attempt_dir, 'runtime_compatibility.json', json.dumps(compatibility_report, indent=2))
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id, type='log',
                                    name='model_send', payload=selection))
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id, type='log',
                                    name='runtime_compatibility', payload=compatibility_report))
        if agent.coding_agent == "claude_code":
            request = CCRequest(
                prompt=prompt_text,
                system_prompt=dispatch_system,
                model=agent.model,
                thinking=agent.thinking,
                session_id=session_id,
                # absolute: read by the claude subprocess, which runs in repo_root
                session_dir=str((agent_dir / "cc_sessions").resolve()),
                raw_output_path=str((attempt_dir / "raw_output.jsonl").resolve()),
                tools=agent.tools,
                cwd=str(run.repo_root),
                timeout_seconds=timeout,
            )
            result = agent_cc.run(
                request,
                on_event=_event_forwarder(run, phase, agent),
                on_spawn=lambda pid: run.tracer.process_start(
                    run.adw_id, "agent", agent.name, pid,
                    f"{agent.coding_agent} {agent.name} {agent.model}"),
                on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        elif agent.coding_agent == "delegate_codex":
            from .agent_delegate import DelegateRequest, run as relay_run
            request = DelegateRequest(
                prompt=prompt_text, system_prompt=dispatch_system, model=agent.model,
                run_id=run.adw_id, phase_id=phase.phase_id,
                thinking=agent.thinking,
                supported_thinking=tuple(agent.supported_thinking if agent.supported_thinking is not None
                                         else ("low", "medium", "high", "xhigh")),
                session_id=session_id, session_dir=str(attempt_dir / "relay"),
                raw_output_path=str(attempt_dir / "raw_output.jsonl"),
                cwd=str(run.repo_root), baseline=run.source_baseline,
                allowed_paths=[*(agent.writes if agent.writes is not None else ["**"]),
                               run.agent_output_relative + "/**"],
                evidence_requirements=list(getattr(run, "required_requirements", []) or [call.prompt]),
                output_contract=call.output_type.model_json_schema(),
                task_context=user_text, tools=agent.tools,
                sandbox=agent.delegate_sandbox, sandbox_scope=["**"] if agent.delegate_sandbox == "workspace-write" else [],
                timeout_seconds=timeout, relay_path=agent.delegate_relay_path)
            result = relay_run(request, on_event=_event_forwarder(run, phase, agent),
                on_spawn=lambda pid: run.tracer.process_start(run.adw_id, "agent", agent.name, pid, "delegate_codex"),
                on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        else:
            raise ValueError(f"Unsupported runtime {agent.coding_agent!r}; rerun /kh-setup-models")
        _normalize_cost(result, spent, run)
        cost_known = cost_known and result.cost_known
        run.add_usage(result.tokens, result.cost, result.cost_known)
        spent.merge(result.usage)
        latest = result
        execution_budget.remaining_timeout(run, agent.timeout_seconds)
        return result

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    tree_before = permissions.snapshot(run)

    run.agent_baseline = tree_before

    def account_claude_failure(error):
        nonlocal latest
        partial = (error.result if isinstance(error, agent_cc.CCRunError)
                   else getattr(error, 'kh_claude_usage_result', None))
        if isinstance(partial, agent_cc.CCRunResult):
            partial.cost_known = False
            _normalize_cost(partial, spent, run)
            run.add_usage(partial.tokens, partial.cost, False)
            spent.merge(partial.usage)
            latest = partial

    def send(prompt_text):
        nonlocal cost_known, session_id, latest
        workspace.require_role_cleanup(run)
        # Every correction re-enters the same session, so occupancy is decided
        # here — before the send that would overflow, not after it failed. The
        # check runs ahead of the dispatch bookkeeping below: a refusal spends
        # nothing, so it must not mark this phase's cost uncertain.
        prompt_text = prepare_send(prompt_text)
        before_tokens = int(getattr(latest, "context_tokens", 0) or 0) if latest else 0
        rotated = False
        if send_number:
            rotation = context_guard.before_correction(run, phase, agent, latest,
                                                       _rotated_session_id(run, agent))
            if rotation:
                rotated = True
                session_id = rotation.session_id
                prompt_text = rotation.brief(user_text, prompt_text)
                run.console.note(f"context guard: {agent.name} rotated to a fresh session at "
                                 f"{rotation.occupancy_percent:.1f}% occupancy")
        settings = getattr(getattr(run, "cfg", None), "execution", None) or ExecutionSettings()
        retries = 0
        while True:
            if retries:
                prompt_text = prepare_send(prompt_text)
            failure = None
            try:
                result = raw_send(prompt_text, retry=retries > 0)
            except execution_budget.BudgetExceeded as error:
                workspace.remember_role_cleanup(run, error, phase_id=phase.phase_id, agent=agent.name)
                account_claude_failure(error)
                cost_known = False
                run.add_usage(0, 0.0, False)
                raise
            except (RoleTimeout, RoleCleanupError) as error:
                workspace.remember_role_cleanup(run, error, phase_id=phase.phase_id, agent=agent.name)
                # Completed priced turns before a cutoff still count. The
                # interrupted turn makes the overall total explicitly partial.
                partial = error.result
                partial.cost_known = False
                _normalize_cost(partial, spent, run)
                run.add_usage(partial.tokens, partial.cost, False)
                spent.merge(partial.usage)
                latest = partial
                cost_known = False
                run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                    type='agent_end', name=agent.name, tokens=spent.total_tokens,
                    payload={'cost': spent.total_cost, 'cost_known': False, 'usage': spent.model_dump(),
                             'interrupted': True, 'terminal_success': False,
                             'context_tokens': partial.context_tokens, 'context_window': partial.context_window}))
                failure = error
            except Exception as error:
                workspace.remember_role_cleanup(run, error, phase_id=phase.phase_id, agent=agent.name)
                account_claude_failure(error)
                # A dispatched request may have spent money before failing.
                # Record that uncertainty before permission checks can replace
                # its transport exception with a more specific breach.
                cost_known = False
                run.add_usage(0, 0.0, False)
                failure = error
            except BaseException as error:
                workspace.remember_role_cleanup(run, error, phase_id=phase.phase_id, agent=agent.name)
                account_claude_failure(error)
                cost_known = False
                run.add_usage(0, 0.0, False)
                raise
            finally:
                # Includes exceptions, parse-retries, timeout and cancellation paths.
                run.actual_changed_paths = permissions.enforce(run, phase, agent, tree_before)
            # A forbidden infrastructure command that actually executed changes no
            # file, so file enforcement above cannot see it (SYS-05).
            for record in list(breaches):
                infrastructure.check_record(record)
            if failure is None and (result.returncode != 0
                                    or result.reported_error or not result.terminal_success):
                failure = RuntimeError(f"{agent.name} transport failed: exit={result.returncode}, "
                                       f"error={result.reported_error}, terminal={result.terminal_success}")
            if failure is None:
                break
            # Provider outages (overloaded, at capacity, 5xx, dropped connection)
            # are waited out in the same session; every other failure stops here.
            reason = transient.classify(failure, invocation_dir / f"attempt-{send_number:03d}")
            if not reason or retries >= settings.transient_retries:
                if reason:
                    raise RuntimeError(f"{failure} [provider outage persisted after {retries} "
                                       f"retr{'y' if retries == 1 else 'ies'}: {reason}]") from failure
                raise failure
            retries += 1
            wait = transient.delay(retries, settings.transient_retry_delay_seconds)
            execution_budget.reserve_transient_retry(run, wait)
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type="log", name="transient_retry",
                                         payload={"agent": agent.name, "retry": retries,
                                                  "of": settings.transient_retries,
                                                  "wait_seconds": wait, "reason": reason,
                                                  "attempt": send_number}))
            run.console.note(f"{agent.name}: provider outage ({reason}); retry {retries}/"
                             f"{settings.transient_retries} in the same session after {wait}s")
            time.sleep(wait)
        # A window that shrank without the controller rotating it means the
        # runtime compacted on its own; that is unbudgeted and worth recording.
        context_guard.note_runtime_compaction(run, phase, agent, before_tokens, not rotated, result)
        return result

    result = send(user_text)
    envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    required_gates = [gates.artifacts_exist]
    if call.output_type.__name__ == "BuildOutput":
        required_gates.append(gates.diff_matches_claims)
        if ledger is not None:
            required_gates.append(command_ledger.gate)
    if call.output_type.__name__ == "ReviewOutput": required_gates.append(gates.verdict_consistent)
    if getattr(run, "evidence_policy", None) is not None:
        required_gates.append(dispositions.claims_gate)
    effective_gates = list(dict.fromkeys([*required_gates, *call.gates]))
    # Gates that judge only the REPORT (or evidence bookkeeping the agent can fix
    # by re-running a command): their corrections are not charged to retries.
    report_gates = {gates.diff_matches_claims, command_ledger.gate, dispositions.claims_gate}
    history: list[dict] = []
    run.gate_history = history
    # claim gates — violations flow back into the SAME session as corrections.
    # A wrong changed-file CLAIM is not a defect in the work: the controller
    # measured the real set and names it, so those corrections are not charged
    # to the phase's retries (a bounded few), and a report that stays wrong
    # still fails.
    charged = free = 0
    gate_attempt = 0
    while True:
        gate_attempt += 1
        violations = []
        report_only = True
        for gate in effective_gates:
            report = _as_report(gate(envelope, run))
            found = report.violations
            run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="gate_fail" if found else "gate_pass", name=gate.__name__,
                payload={"attempt": gate_attempt, "violations": found,
                         "checks": [c.model_dump() for c in report.checks]}))
            run.console.gate_result(gate.__name__, report)
            history.append({"attempt": gate_attempt, "gate": gate.__name__, "ok": not found,
                            "violations": found})
            if found and gate not in report_gates:
                report_only = False
            violations.extend(found)
        if not violations:
            break
        if report_only and free < REPORT_ONLY_CORRECTIONS:
            free += 1
        elif charged >= phase.params.retries:
            prompts.save(invocation_dir, "gate_history.json", json.dumps(gate_summary(history), indent=2))
            raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                              + "\n- ".join(violations))
        else:
            charged += 1
        phase.attempt = gate_attempt
        run.console.retry(agent.name, gate_attempt,
                          phase.params.retries + (REPORT_ONLY_CORRECTIONS if report_only else 0),
                          f"{len(violations)} gate violation(s)")
        correction = ("Your previous response failed validation:\n- "
                      + "\n- ".join(violations)
                      + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    try:
        touched = permissions.enforce(run, phase, agent, tree_before)
    except permissions.PermissionBreach as breach:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="error", name="permission_breach",
                                     payload={"agent": agent.name, "error": str(breach),
                                              "writes": agent.writes,
                                              "protected_files": run.cfg.defaults.protected_files}))
        raise
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    prompts.save(invocation_dir, "gate_history.json", json.dumps(gate_summary(history), indent=2))
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported failure: {envelope.summary}")
    envelope = run.promote(envelope)
    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, session_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.save_agent_map(agent.name, {"session_id": session_id, "model": agent.model, "phase_id": phase.phase_id,
                                    "coding_agent": agent.coding_agent})
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": spent.total_cost, "cost_known": cost_known,
                                          "usage": spent.model_dump(),
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost, cost_known)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


# ── internals ────────────────────────────────────────────────────────────────

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_session_id(run, agent: AgentConfig) -> str:
    entry = run.agent_map.get(agent.name)
    if (entry and entry.get("phase_id") == getattr(run, "active_phase_id", None)
            and entry.get("model") == agent.model
            and entry.get("coding_agent") == agent.coding_agent):
        return entry["session_id"]           # rejoin the existing context window
    if agent.coding_agent == "claude_code":
        return str(uuid.uuid4())             # Claude Code takes UUID session ids only
    return f"kh-{run.adw_id}-{agent.name}-{new_id(4)}"


def _rotated_session_id(run, agent: AgentConfig) -> str:
    """A deliberately new session id: rotation must not rejoin the old window."""
    if agent.coding_agent == "claude_code":
        return str(uuid.uuid4())             # Claude Code takes UUID session ids only
    return f"kh-{run.adw_id}-{agent.name}-{new_id(4)}"


def _event_forwarder(run, phase: Phase, agent: AgentConfig):
    """One tool_call event per real tool call, with its exact args and result."""
    ledger = vars(run).get("command_ledger") if hasattr(run, "__dict__") else None
    hooks = {"on_open": ledger.opened, "on_close": ledger.closed} if ledger is not None else {}
    tracker = agent_cc.ToolCallTracker(**hooks) if agent.coding_agent == "claude_code" else None

    def forward(event: dict) -> None:
        if ledger is not None and event.get("type") == "delegate_event":
            ledger.codex_event(event.get("event") or {})
        if event.get("type", "").startswith("delegate_"):
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type="log", name=event["type"], payload=event))
            return
        if event.get("type") in {"claude_usage_accounting"}:
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type="log", name=event["type"], payload=event))
            return
        records = tracker.observe(event) if tracker is not None else []
        if not records:
            return
        if isinstance(records, dict): records = [records]
        for record in records:
            # The call's span rides the columns; duration_ms stays in the payload as
            # the coding agent's own authoritative number.
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type="tool_call", name=record.pop("label"),
                                         started_at=record.pop("started_at", None),
                                         ended_at=record.pop("ended_at", None),
                                         payload={**record, "agent": agent.name}))
    return forward


# Shared parsing keeps terminal artifact bindings and typed handoffs aligned.
from .terminal_evidence import extract_json as _extract_json


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        history = vars(run).get("gate_history") if hasattr(run, "__dict__") else None
        if history:
            record["gate_attempts"] = gate_summary(history)
        directory = run.session_dir / agent_name / "phases" / phase.phase_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"envelope-{new_id(12)}.json").write_text(json.dumps(record, indent=2))
