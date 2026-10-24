"""The Run object: config + adw_id + agent_map + tracer + console, bound once.

`run.phase(PhaseParams(...))` is the ONE phase primitive — a context manager
for all three kinds (engineer, agent, code). Success must be earned: every
phase defaults to fail; only a clean exit flips it (agent phases additionally
require a parsed envelope + green gates, enforced inside ph.call).
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

from . import agents, git_helper
from .console import Console
from .data_types import AgentCall, EnvelopeBase, EventRecord, Phase, PhaseParams
from .utils import ensure_dir, now_iso


class PhaseHandle:
    def __init__(self, run: "Run", phase: Phase):
        self.run = run
        self.phase = phase

    def log(self, **payload) -> None:
        self.run.tracer.event(EventRecord(adw_id=self.run.adw_id,
                                          phase_id=self.phase.phase_id,
                                          type="log", name=self.phase.params.name,
                                          payload=payload))
        self.run.console.note(", ".join(f"{k}: {v}" for k, v in payload.items()))
        if self.phase.params.kind == "engineer" and "input" in payload:
            if self.run.scope_contract is not None and str(payload['input']) != self.run.scope_contract.original_request:
                raise ValueError('Request differs from saved task scope; use a new run')
            self.run.tracer.session_request(self.run.adw_id, str(payload["input"]))
            if not self.run.required_requirements:
                self.run.required_requirements = [str(payload["input"])]

    def call(self, call: AgentCall) -> EnvelopeBase:
        if self.phase.params.kind != "agent":
            raise RuntimeError("ph.call() is only valid inside an agent phase")
        return agents.execute(self.run, self.phase, call)


class Run:
    def __init__(self, cfg, adw_id: str, tracer, engineer: str):
        self.cfg = cfg
        self.required_requirements: list[str] = []
        self.execution_budget = None
        self.execution_policy = None
        self.scope_contract = None
        self.scope_review_paths = None
        self._lock = None
        self.adw_id = adw_id
        self.tracer = tracer
        self.console = Console(tracer, adw_id)
        self.engineer = engineer
        self.phases: list[Phase] = []
        self.tokens = 0
        self.cost = 0.0
        self.cost_known = True
        previous_usage = tracer.conn.execute(
            'SELECT total_tokens,total_cost,cost_known FROM sessions WHERE adw_id=?', (adw_id,)).fetchone()
        if previous_usage:
            self.tokens, self.cost = previous_usage[0] or 0, previous_usage[1] or 0.0
            self.cost_known = previous_usage[2] == 1
        self._seq = tracer.max_phase_seq(adw_id)   # a joined run continues the sequence
        self.repo_root = git_helper.repo_root()    # where every agent is spawned to work
        self.session_dir = ensure_dir(Path(cfg.defaults.data_dir) / "sessions" / adw_id)
        self.context_handoff_dir = ensure_dir(self.session_dir / "context_handoff")
        if (self.session_dir / "acceptance.json").is_file():
            from .scope import _read_json
            self.required_requirements = _read_json(self.session_dir / "acceptance.json")
        if (self.session_dir / "scope.json").exists() or (self.session_dir / "scope.json").is_symlink():
            from .scope import load_contract
            self.scope_contract = load_contract(self.session_dir / "scope.json")
        self._agent_map_path = self.session_dir / "agent_map.json"
        self.agent_map: dict = (json.loads(self._agent_map_path.read_text())
                                if self._agent_map_path.exists() else {})

    # ── agent map (adw_id -> per-agent coding-agent session ids) ────────────
    def save_agent_map(self, agent: str, entry: dict) -> None:
        self.agent_map[agent] = entry
        self._agent_map_path.write_text(json.dumps(self.agent_map, indent=2))

    # ── usage (run totals mirror what the tracer accumulates in sqlite) ─────
    def add_usage(self, tokens: int, cost: float, cost_known: bool = False) -> None:
        self.tokens += tokens
        self.cost += cost
        self.cost_known = self.cost_known and cost_known
        self.tracer.session_add_usage(self.adw_id, tokens, cost, cost_known)

    # ── the phase primitive ─────────────────────────────────────────────────
    @contextmanager
    def phase(self, params: PhaseParams):
        self._seq += 1
        phase = Phase(phase_id=f"{self.adw_id}_{self._seq:02d}_{params.name}",
                      adw_id=self.adw_id, seq=self._seq, params=params,
                      status="running", started_at=now_iso())
        self.phases.append(phase)
        self.tracer.phase_upsert(phase)
        self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                      type="phase_start", name=params.name,
                                      payload={"kind": params.kind, "owner": params.owner,
                                               "description": params.description}))
        self.console.phase_started(phase)
        clock = time.monotonic()
        try:
            yield PhaseHandle(self, phase)
        except BaseException as error:
            phase.status = "fail"                      # success must be earned
            phase.error = str(error)[:1000]
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="error", name=params.name,
                                          payload={"error": phase.error}))
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "fail"}))
            self.tracer.phase_upsert(phase)
            self.console.phase_ended(phase, time.monotonic() - clock)
            if not getattr(self, 'defer_phase_failure_finish', False):
                self._finish_trace(False)
                self.console.session_finished(False, self.tokens, self.cost,
                                              self.cfg.observability.db, self.cost_known)
            # The task controller must record failure before releasing the lock.
            raise
        else:
            phase.status = "success"
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "success"}))
            self.tracer.phase_upsert(phase)
            self.console.phase_ended(phase, time.monotonic() - clock)

    # ── run outcome ─────────────────────────────────────────────────────────
    def finish(self, accepted: bool = True, reason: str = "", *, recovered_phases=()) -> int:
        """Finalize the run and return its exit code. Call this exactly once.

        Two criteria, not one. Every phase must have passed, AND the ADW's own
        acceptance test must hold. They are different questions on purpose: a
        test phase that ran the suite did its job even when the suite came back
        red, so the PHASE succeeds while the RUN must not.

        This replaces a `succeeded` property that answered only the first
        question — and, being a property with side effects, wrote the session
        status and printed the banner before the caller's `and test.passed` was
        ever evaluated. A run whose suite never passed was recorded green in the
        db, on the terminal, and in the UI while exiting 1. Anyone reading the
        trace saw success; only a CI job checking `$?` saw the truth. One call
        now settles the db, the banner, and the exit code together, so the three
        cannot disagree.
        """
        recovered = set(recovered_phases)
        eligible = {p.phase_id for p in self.phases if p.status == 'fail'
                    and p.params.owner == 'builder' and p.params.kind == 'agent'}
        if not recovered <= eligible:
            raise ValueError('Only recorded failed builder phases may be settled by guarded recovery')
        guarded_accepted = any(p.params.name == 'guarded_task' and p.status == 'success' for p in self.phases)
        phases_ok = bool(self.phases) and all(p.status == "success" or
            (guarded_accepted and p.phase_id in recovered) for p in self.phases)
        ok = phases_ok and accepted
        if ok and recovered:
            self.tracer.event(EventRecord(adw_id=self.adw_id, type='log', name='recovered_phases',
                payload={'phases': sorted(recovered), 'accepted': True,
                         'note': 'Failed attempts remain recorded; the guarded candidate passed later checks and review.'}))
        if phases_ok and not accepted:
            note = reason or "the run's acceptance criterion was not met"
            self.tracer.event(EventRecord(
                adw_id=self.adw_id,
                phase_id=self.phases[-1].phase_id if self.phases else "",
                type="error", name="not_accepted", payload={"reason": note}))
            self.console.note(f"not accepted: {note}")
        from . import execution_budget
        if not ok:
            execution_budget.record_stop(self, reason or "TASK_NOT_ACCEPTED")
        budget_summary = execution_budget.summary(self)
        if budget_summary:
            self.execution_budget.save()
            self.console.note("Execution budget: " + json.dumps(budget_summary))
        self._finish_trace(ok)
        self.console.session_finished(ok, self.tokens, self.cost, self.cfg.observability.db, self.cost_known)
        from . import session
        session.restore_signal_handlers(self)
        if self._lock: self._lock.close()
        return 0 if ok else 1

    def _finish_trace(self, ok):
        if getattr(self, 'role_cleanup_pending', None):
            self.tracer.session_finish(self.adw_id, ok=ok, preserve_agents=True)
        else:
            self.tracer.session_finish(self.adw_id, ok=ok)

    def record_observations(self, envelope, phase):
        if not envelope.observations:
            return
        import importlib.util
        helper = self.repo_root / ".claude/skills/task-observer/scripts/observer_store.py"
        try:
            spec = importlib.util.spec_from_file_location("kh_observer", helper)
            module = importlib.util.module_from_spec(spec)
            import sys
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            for item in envelope.observations:
                fields = {k: item[k] for k in ("title", "issue", "suggestion", "principle", "skill")}
                module.record_observation(self.repo_root, **fields,
                    evidence=item.get("evidence", []), run_id=self.adw_id,
                    phase=phase.phase_id, source_revision=git_helper.rev() if git_helper.ref_exists("HEAD") else "unversioned")
        except Exception as error:
            # Advisory persistence must never turn a completed task into failure.
            self.console.note(f"Observation capture warning: {error}")
