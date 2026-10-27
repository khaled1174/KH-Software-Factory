#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Guarded task: route -> scope -> staged plan/build/check/review -> promotion."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from contextlib import contextmanager

from adw_modules import agents, delta, gates, permissions, quality, routing, scope, session, utils, project
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams, PlanOutput, ReviewOutput
from adw_modules.workspace import AgentWorkspace
from adw_modules import planning, project_context, verification_evidence, execution_budget, intent, owner_review, floor_guard
from adw_modules import resume
from adw_modules import dispositions, evidence_packet, findings, infrastructure, timing
from adw_modules.workspace import exclusions
from adw_modules.execution_policy import resolve_execution_policy, resolve_bootstrap_policy, validate_light_paths
from adw_modules.process_io import RoleTimeout
from adw_modules import documentation


@contextmanager
def durable_stage(run, phase, builder):
    with AgentWorkspace(run, phase, builder) as staged:
        staged.evidence_published = False
        try:
            yield staged
        except BaseException as error:
            # Unaccepted work outlives the run: the next run of this task continues it.
            keep = getattr(staged, 'resume_save', None)
            if keep is not None and not getattr(staged, 'promoted', False):
                try:
                    kept = keep(str(error) or type(error).__name__)
                    run.console.note(f"kept this run's unaccepted work for the next run "
                                     f"({len(kept['paths'])} paths) in {resume.WORK_DIR / kept['task']}")
                except Exception as keep_error:
                    run.console.note(f'Unaccepted work could not be kept for the next run: {keep_error}')
            raise
        finally:
            if not staged.evidence_published:
                from adw_modules.evidence import publish_staged_evidence
                try:
                    publish_staged_evidence(run, staged.view)
                except Exception as error:
                    run.console.note(f'Failed candidate evidence could not be fully preserved: {error}')


def kept_paths(work):
    """Every candidate change to keep for a later run, directories included."""
    current = permissions.snapshot(work.view)
    output = work.view.agent_output_relative
    return sorted(p for p in permissions.changed_paths(work.view.agent_baseline, current)
                  if not p.startswith('@git/') and p != output and not p.startswith(output + '/'))


def last_feedback(envelope) -> str:
    """What the last failed check said, for the builder that continues the work.

    A review's verdict travels separately and in full as the last review record
    (SYS-06); here it would only be a clipped duplicate.
    """
    if envelope is None or isinstance(envelope, ReviewOutput):
        return ''
    parts = [getattr(envelope, 'summary', '') or '']
    for field in ('blocking', 'failures'):
        value = getattr(envelope, field, None)
        if value:
            parts.append(json.dumps(value, ensure_ascii=False, default=str)[:3000])
    return ' '.join(p for p in parts if p)


def source_paths(work):
    current = permissions.snapshot(work.view)
    touched = permissions.changed_paths(work.view.agent_baseline, current)
    output = work.view.agent_output_relative
    # A directory that merely appears or disappears is covered by its contents;
    # a kind change or a mode change on an existing directory is reviewed itself.
    return sorted(p for p in touched if not p.startswith('@git/')
                  and p != output and not p.startswith(output + '/')
                  and not permissions.structural_directory(p, work.view.agent_baseline, current))


def spend(run) -> dict:
    """What this run spent, for the owner's report. Reported only: cost never stops work."""
    usd = round(float(getattr(run, 'cost', 0.0) or 0.0), 4)
    known = bool(getattr(run, 'cost_known', False))
    tokens = int(getattr(run, 'tokens', 0) or 0)
    return {'usd': usd, 'complete': known, 'tokens': tokens,
            'note': 'all sends priced' if known else
                    'partial: some sends report no price (for example Gemini CLI or a subscription)'}


def spend_line(run) -> str:
    cost = spend(run)
    amount = f"${cost['usd']:.2f}" + ('' if cost['complete'] else ' (partial)')
    return f"Cost: {amount}, {cost['tokens']:,} tokens"


def task_contract(task):
    return scope.ScopeContract(original_request=task['request'], deliverables=task['deliverables'],
        allowed_paths=task['allowed_paths'], constraints=task['constraints'], non_goals=task['non_goals'],
        required_checks=task['required_checks'])


def categories_of(reviewed) -> list[str]:
    """One action category per blocking item; an item the reviewer did not classify is a code change."""
    given = list(getattr(reviewed, 'blocker_categories', []) or [])
    if len(given) != len(reviewed.blocking):
        return ['code_change'] * len(reviewed.blocking)
    return given


def blocker_route(reviewed) -> str:
    """What a rejection needs next: any code change wins; otherwise the controller's evidence path."""
    categories = set(categories_of(reviewed)) or {'code_change'}
    if 'code_change' in categories:
        return 'code_change'
    for route in ('infrastructure_or_provider', 'owner_decision', 'historical_evidence_unavailable'):
        if route in categories:
            return route
    return 'evidence_delivery'


def advisory_receipt(run, view, work_key, kept, reviewed, candidate, checked, decisions) -> Path:
    """A standalone review of the saved candidate: evidence only, never acceptance (FIX-04)."""
    receipts = sorted((evidence_packet.inputs_dir(view) / 'receipts').glob('*.json'))
    latest = receipts[-1] if receipts else None
    record = {'schema_version': 1, 'kind': 'advisory_review', 'run_id': run.adw_id, 'task': work_key,
              'candidate_from_run': kept.get('run_id'), 'candidate_sha256': candidate,
              'candidate_paths': kept.get('paths'), 'packet_identity': dict(getattr(view, 'packet_identity', {})),
              'evidence_receipt': latest.name if latest else None,
              'evidence_receipt_sha256': hashlib.sha256(latest.read_bytes()).hexdigest() if latest else None,
              'evidence_policy_digest': run.evidence_policy['digest'],
              'decisions_in_force': [d['id'] for d in decisions],
              'checks': [c.model_dump() for c in checked.checks] if checked is not None else [],
              'approved': bool(reviewed.approved), 'summary': reviewed.summary, 'blocking': list(reviewed.blocking),
              'blocker_categories': list(getattr(reviewed, 'blocker_categories', []) or []),
              'findings': [f.model_dump() for f in reviewed.findings],
              'promoted': False, 'task_state_changed': False,
              'note': 'Advisory: this review does not promote source or change task acceptance. A later promotion '
                      'must go through /kh-start --review-existing (without --advisory) on unchanged bytes.'}
    target = resume.work_dir(run.repo_root, work_key) / 'reviews' / f'{run.adw_id}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    return target


def review_record(run, phase, reviewed, candidate) -> dict:
    """The last real verdict, kept whole for the next run (SYS-06)."""
    return {'run_id': run.adw_id, 'phase': phase, 'approved': bool(reviewed.approved),
            'summary': reviewed.summary, 'blocking': list(reviewed.blocking),
            'repair_paths': list(reviewed.repair_paths),
            'findings': [f.model_dump() for f in reviewed.findings], 'candidate_sha256': candidate,
            'artifacts': list(reviewed.artifacts)}


def reuse_checks(view, saved, work):
    """Saved check results for byte-identical candidate source, with their logs re-delivered (SYS-10)."""
    from adw_modules.data_types import QualityCheckResult, QualityResult
    result = saved['verification_result']
    names = {check.get('name') for check in result.get('checks', [])}
    if names != set(view.cfg.defaults.required_quality_checks):
        return None
    for name in view.cfg.defaults.required_quality_checks:
        matching = [check for check in result.get('checks', []) if check.get('name') == name]
        if len(matching) != 1:
            return None
        try:
            if shlex.split(matching[0].get('command', '')) != list(view.cfg.defaults.quality_checks[name]):
                return None
        except (ValueError, TypeError):
            return None
    checks = []
    for check in result.get('checks', []):
        source = work / 'checks' / (check['name'] + '.log')
        target = Path(view.context_handoff_dir) / 'quality' / f"reused_{check['name']}" / 'command.log'
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source.is_file():
            return None
        target.write_bytes(source.read_bytes())
        checks.append(QualityCheckResult.model_validate({**check, 'output_artifact': str(target),
                                                          'reused_from_run': saved.get('run_id', ''),
                                                          'candidate_sha256': saved['candidate_sha256']}))
    if not checks or not all(c.passed for c in checks):
        return None
    return QualityResult(passed=True, checks=checks, failures=[], artifacts=[c.output_artifact for c in checks])


def context_preflight(review_existing, refresh_context):
    if refresh_context and review_existing:
        raise ValueError('--refresh-context cannot accompany --review-existing: the saved candidate binds its original context')
    context_state = project_context.inspect_context(Path.cwd())
    if review_existing and context_state['status'] != 'ready':
        raise ValueError('REVIEW_EXISTING_REQUIRES_READY_CONTEXT: review-only execution cannot prepare '
                         'or refresh project context; resolve it separately before reviewing the saved candidate')
    prepare_context = context_state['status'] == 'missing' or (
        refresh_context and context_state['status'] == 'stale')
    return context_state, prepare_context


def task_roles(cfg, prepare_context, review_existing):
    roles = ['planner', 'plan_reviewer', 'builder', 'reviewer']
    if cfg.execution.documentation.enabled and not review_existing:
        roles.append('documenter')
    if prepare_context:
        roles.append('project_architect')
    return roles


def preflight(prompt, cfg, decision, contract, mode, review_existing, advisory, owner_evidence, refresh_context,
              *, accepted_intent=None, context=None, validate_agents=True):
    """Resolve common task inputs once, without starting or binding a run."""
    if not contract.review_required or contract.original_request != prompt:
        raise ValueError('Guarded tasks require review and matching original request')
    if advisory and not review_existing:
        raise ValueError('--advisory applies to --review-existing only')
    owner_evidence = [Path(p) for p in owner_evidence or ()]
    for path in owner_evidence:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'--evidence must name an existing regular file: {path}')
    if review_existing:
        if not cfg.execution.resume_failed_builds:
            raise ValueError('REVIEW_EXISTING_UNAVAILABLE: execution.resume_failed_builds is off, so no candidate is kept')
        # A review of an existing candidate never starts a builder round.
        decision = {**decision, 'max_repairs': 0}
    accepted_intent = accepted_intent if accepted_intent is not None else intent.load(Path.cwd(), prompt)
    unknown = owner_review.unknown_approvals(cfg.execution.owner_review, accepted_intent['approved_sensitive'])
    if unknown:
        raise ValueError('Intent approves unknown sensitive-change categories: ' + ', '.join(unknown) +
                         '; known: ' + ', '.join(sorted(owner_review.known_categories(cfg.execution.owner_review))))
    cfg = cfg.model_copy(deep=True)
    cfg.defaults.required_quality_checks = list(dict.fromkeys(
        [*cfg.defaults.required_quality_checks, *contract.required_checks]))
    if type(decision.get('max_repairs')) is not int or not 0 <= decision['max_repairs'] <= 5:
        raise ValueError('Invalid bounded repair budget')
    context_state, prepare_context = context or context_preflight(review_existing, refresh_context)
    required_roles = task_roles(cfg, prepare_context, review_existing)
    for role in required_roles:
        if role != 'builder':
            agents.resolve(cfg, role).writes = []
    if validate_agents:
        agents.validate(cfg, required_roles)
        quality.require_configured(cfg, cfg.defaults.required_quality_checks)
    from adw_modules import runtime_compatibility
    builder_preflight = ({'status': 'not_required', 'blocked': [],
                          'reason': 'Review-only continuation does not dispatch a builder'} if review_existing else
                         runtime_compatibility.structural(agents.resolve(cfg, 'builder'), cfg))
    policy = resolve_execution_policy(cfg, decision, mode)
    return (cfg, decision, policy, accepted_intent, context_state, prepare_context, owner_evidence,
            builder_preflight, required_roles)


def execute_task(prompt, cfg, decision, contract, adw_id=None, task_id=None, mode="full", repo_lock=None,
                 review_existing=False, advisory=False, owner_evidence=(), refresh_context=False):
    """Run a task directly through the same preflight used by the CLI."""
    prepared = preflight(prompt, cfg, decision, contract, mode, review_existing, advisory,
                         owner_evidence, refresh_context)
    return _execute_task(prompt, contract, prepared, adw_id, task_id, mode, repo_lock,
                         review_existing, advisory)


class TaskState:
    """One task's live state, shared by recovery callbacks and phase helpers."""
    def __init__(self, run, cfg, decision, contract, prompt, task_id, review_existing, advisory, policy, accepted_intent, owner_decisions):
        self.run = run
        self.cfg = cfg
        self.decision = decision
        self.contract = contract
        self.prompt = prompt
        self.task_id = task_id
        self.review_existing = review_existing
        self.advisory = advisory
        self.policy = policy
        self.accepted_intent = accepted_intent
        self.owner_decisions = owner_decisions
        self.handoffs = []
        self.observations = []
        self.floor = []
        self.owner_flags = []
        self.recoveries = []
        self.recovered_build_phases = []
        self.leased = self.source_promoted = self.task_completed = self.accepted = False
        self.kept = self.last_review = self.build = self.verified_source = self.checked = self.previous = None
        self.existing_rejected = False


def _record_handoff(state: TaskState, envelope, phase):
    handoff = {'phase': phase, 'summary': envelope.summary, 'status': envelope.status, 'artifacts': [
                   Path(p).relative_to(state.view.repo_root).as_posix() if Path(p).is_absolute() else p
                   for p in envelope.artifacts
                   if not Path(p).is_absolute() or Path(p).is_relative_to(state.view.repo_root)]}
    # Attempt-level gate results: a correction that passed never hides the
    # failed attempt before it (SYS-07).
    recorded = getattr(state.view, 'gate_history_by_phase', None) or {}
    if recorded and not phase.endswith('_reused'):
        handoff['gates'] = [row['text'] for row in list(recorded.values())[-1]]
    state.handoffs.append(handoff)
    if state.task_id and not state.advisory:
        project.record_handoff(state.run.repo_root, state.task_id, state.run.adw_id, handoff)


def _progress(state: TaskState, status, **detail):
    if state.task_id and not state.advisory:
        project.transition(state.run.repo_root, state.task_id, state.run.adw_id, status,
                           detail={'handoffs': list(state.handoffs), **detail})


def _checkpoint(state: TaskState):
    return (quality.workable_fingerprint(state.view), tuple(sorted(
        fid for fid, entry in state.registry.findings.items() if entry['state'] == 'resolved')),
        planning._content_digest(state.plan))


def _recover(state: TaskState, reason, *, require_progress=False):
    current_checkpoint = _checkpoint(state)
    if require_progress and current_checkpoint == state.window_checkpoint:
        raise RuntimeError('RECOVERY_NO_PROGRESS: candidate and finding resolutions did not improve; '
                           'unaccepted work and findings kept')
    execution_budget.record_recovery(state.view, reason)
    row = {'number': state.run.execution_budget.recoveries, 'reason': reason,
           'candidate_sha256': current_checkpoint[0], 'resolved_findings': list(current_checkpoint[1]),
           'cost': spend(state.run), 'execution': execution_budget.summary(state.run)}
    state.recoveries.append(row)
    scope._atomic_json(state.run.session_dir / 'recoveries.json', state.recoveries)
    with state.run.phase(PhaseParams(name=f'recovery_{row["number"]}', kind='code', owner='controller',
                              description='Continue permitted work under the original task limits')) as ph:
        ph.log(**row)
    state.run.console.note(f"continuing {reason}; recovery {row['number']}/{state.policy.recovery_rounds}; "
                     + spend_line(state.run) + ' (reported only)')
    state.window_checkpoint = current_checkpoint


def _revise_paths(state: TaskState, required_paths, reason):
    # A reviewer request is evidence, not write authority. Both
    # the original task and the builder's role must permit it.
    if state.contract.allowed_paths is None:
        raise scope.ScopeViolation('OWNER_SCOPE_REQUIRED: extending a plan requires an explicit owner scope')
    required_paths = planning.revision_paths(state.view, state.plan, state.contract, required_paths)
    _recover(state, 'reviewed_plan_revision')
    revised, approval = planning.revise_approved_plan(
        state.view, state.plan, state.plan_approval, state.contract, state.context_packet,
        required_paths=required_paths, reason=json.dumps(state.last_review, ensure_ascii=False),
        on_handoff=lambda envelope, phase: _record_handoff(state, envelope, phase))
    state.view.scope_contract = scope.activate_revised_contract(state.view, state.contract, revised, approval, state.context_packet)
    validate_light_paths(state.cfg, state.policy, revised.allowed_paths)
    state.plan, state.plan_approval = revised, approval
    if state.context_packet.get('conditional_documents'):
        state.run.project_context_packet = state.view.project_context_packet
        state.understanding = project_context.write_understanding(state.run, state.view.project_context_packet)
    state.plan_content = planning._content_digest(state.plan)
    evidence_packet.deliver_plan(state.view, state.plan, state.plan_approval, state.run.session_dir)
    state.view.packet_identity = {'plan_digest': approval.plan_digest, 'plan_content_digest': state.plan_content}
    state.build_prompt = planning.build_brief(state.prompt, approval)
    if state.kept:
        state.build_prompt += '\n\n' + resume.brief(state.kept, state.info)
    state.checked, state.verified_source = None, None
    if state.cfg.execution.resume_failed_builds:
        resume.write_plan(state.run.repo_root, state.work_key, state.plan, state.info if state.kept else None)
    # The reviewed amendment already spent one recovery window;
    # crossing the old boundary must not spend it a second time.
    state.window_end += state.window_size


def _resume_save(state: TaskState, reason):
    # Paths the effective scope refuses are never kept; they are named
    # with their owner-baseline identity instead (SYS-06).
    keep, refused = [], list(getattr(state.view, 'rejected_paths', None) or [])
    now = permissions.snapshot(state.view)
    for rel in kept_paths(state.staged):
        # Directories are covered by their contents, as in enforcement.
        if any(e is not None and e.kind == 'directory' for e in
               (state.view.task_baseline.entries.get(rel), now.entries.get(rel))):
            keep.append(rel)
            continue
        try:
            scope.check_paths(state.view.scope_contract, [rel])
            keep.append(rel)
        except scope.ScopeViolation as error:
            refused.append({'path': rel, 'reason': str(error)[:500], 'phase': 'task',
                            'baseline': resume.baseline_identity(state.view.task_baseline, rel)})
    saved_checks = None
    if state.verified_source and state.checked is not None and not state.existing_rejected:
        try:
            if quality.source_fingerprint(state.view) == state.verified_source:
                saved_checks = {'run_id': state.run.adw_id, 'candidate_sha256': state.verified_source,
                         'verification_result': state.view.verification_result,
                         'documentation': getattr(state.view, 'documentation_result', None),
                         'logs': {c.name: c.output_artifact for c in state.checked.checks}}
        except Exception:
            saved_checks = None
    return resume.save(
        state.run.repo_root, state.work_key, run_id=state.run.adw_id, reason=reason, owner_digest=state.pristine,
        plan=state.plan, plan_content=state.plan_content, view=state.view, changed=keep,
        excluded=exclusions(state.run), feedback=last_feedback(state.previous),
        last_review=state.last_review, rejected=refused, checked=saved_checks,
        findings=state.registry.to_dict(), evidence_dir=evidence_packet.inputs_dir(state.view),
        last_build=state.build.model_dump() if state.build is not None else None)


def _completion_receipt(state: TaskState, promoted_paths, verified_entries, docs_receipt):
    def relative(path):
        p = Path(path)
        return p.relative_to(state.view.repo_root).as_posix() if p.is_absolute() else p.as_posix()
    return { 'execution': execution_budget.summary(state.run),
        'schema_version': 2, 'task_id': state.task_id, 'run_id': state.run.adw_id,
        'plan_approval_path': planning.approval_path(state.run).resolve().relative_to(state.run.repo_root.resolve()).as_posix(),
        'plan_digest': state.plan_approval.plan_digest,
        'promoted': True, 'checks_passed': True, 'review_approved': True,
        'source_sha256': state.verified_source, 'summary': state.build.summary,
        'documentation': docs_receipt, 'project_context': state.understanding,
        'documentation_path': str((Path(state.run.session_dir) / 'documentation.json').resolve().relative_to(state.run.repo_root.resolve())),
        'changed_files': promoted_paths,
        'source_entries': {path: verified_entries.get(path) for path in promoted_paths},
        'evidence': list(dict.fromkeys(relative(p) for p in
            [*state.plan.artifacts, *state.build.artifacts, *state.checked.artifacts,
             *state.reviewed.artifacts, *docs_receipt.get('artifacts', []), state.view.controller_diff])),
        'handoffs': [{**h, 'artifacts': [relative(p) for p in h['artifacts']]} for h in state.handoffs],
        'checks': [{**c.model_dump(), 'output_artifact': relative(c.output_artifact)} for c in state.checked.checks],
        'review': {'approved': state.reviewed.approved, 'summary': state.reviewed.summary,
                   'blocking': state.reviewed.blocking},
        'owner_review': {'enabled': state.cfg.execution.owner_review.enabled,
                         'mode': state.cfg.execution.owner_review.mode, 'flagged': state.owner_flags,
                         'approved_categories': state.accepted_intent['approved_sensitive'],
                         'intent_path': state.accepted_intent['path'],
                         'intent_accepted_digest': state.accepted_intent['accepted_digest'],
                         'intent_binding': state.accepted_intent['acceptance_binding']}, 'floor_guard': state.floor,
        'cost': spend(state.run), 'recoveries': state.recoveries,
        'recovered_build_phases': state.recovered_build_phases,
        'evidence_policy': {'digest': state.run.evidence_policy['digest'],
                            'decisions': [d['id'] for d in state.owner_decisions]},
        'command_records': dict(getattr(state.view, 'delivered_phases', {}) or {}),
        'gate_attempts': dict(getattr(state.view, 'gate_history_by_phase', {}) or {}),
        'findings': findings.progress(state.registry, state.view.required_requirements, state.reviewed.findings),
        'plan_provenance': 'controller_inputs/' + getattr(state.view, 'active_plan_record_prefix', 'plan') + '/provenance.json',
        'review_existing': bool(state.review_existing),
        'timing': {k: [round(x, 1) for x in v] for k, v in state.clock.observed.items()}, }


def _run_checks(state: TaskState, attempt, reuse_candidate):
    _progress(state, 'verifying', attempt=attempt + 1)
    started = __import__('time').monotonic()
    with state.run.phase(PhaseParams(name=f'checks_{attempt+1}', kind='code', owner='quality',
                               description='Run current configured checks against staged source')) as ph:
        state.checked = None
        if reuse_candidate and quality.source_fingerprint(state.view) == state.kept['checked']['candidate_sha256']:
            # Byte-identical candidate: its recorded checks still apply.
            state.checked = reuse_checks(state.view, state.kept['checked'], resume.work_dir(state.run.repo_root, state.work_key) / 'current')
            ph.log(checks_reused_from=state.kept['checked'].get('run_id') if state.checked else None)
        if state.checked is None:
            state.checked = quality.run_quality(state.view)
        if state.checked.passed:
            extra = verification_evidence.run_adapters(state.view, source_paths(state.staged))
            state.checked = state.checked.model_copy(update={
                'passed': extra.passed, 'checks': [*state.checked.checks, *extra.checks],
                'failures': [*state.checked.failures, *extra.failures],
                'artifacts': [*state.checked.artifacts, *extra.artifacts]})
        ph.log(passed=state.checked.passed, checks=[c.model_dump() for c in state.checked.checks])
    state.clock.done('checks', __import__('time').monotonic() - started)
    if not state.checked.passed:
        _progress(state, 'needs_repair', failures=state.checked.failures)
        state.previous = quality.as_envelope(state.checked, 'required quality checks')
        state.checked = None
        if reuse_candidate:
            raise RuntimeError('REVIEW_EXISTING_CHECKS_FAILED: the saved candidate no longer passes '
                               'the configured checks; run the task normally to repair it')
        return False
    return True


def _measure_delta(state: TaskState, attempt):
    if permissions.dependency_fingerprint(state.view) != state.staged.dependency_baseline:
        raise RuntimeError('VERIFICATION_SOURCE_CHANGED: dependency inputs differ from the owner baseline')
    state.verified_source = quality.source_fingerprint(state.view)
    state.view.packet_identity = {**getattr(state.view, 'packet_identity', {}), 'candidate_sha256': state.verified_source}
    evidence_packet.refresh_receipt(state.view)
    state.view.verification_result = json.loads(state.checked.model_dump_json().replace(str(state.view.repo_root) + '/', ''))
    state.view.verification_result['candidate_sha256'] = state.verified_source
    _progress(state, 'reviewing', checks=[c.model_dump() for c in state.checked.checks])
    state.view.scope_review_paths = source_paths(state.staged)
    # Controller-measured delta: the reviewer judges bytes the
    # controller observed, not the builder's changed_files claim.
    current = permissions.snapshot(state.view)
    diff_path = state.handoff / f'controller_diff_{attempt+1}.patch'
    diff_path.write_text(delta.unified_diff(state.view.agent_baseline, current, state.view.scope_review_paths),
                         encoding='utf-8')
    # Stored repo-relative so nested per-phase workspaces resolve it locally.
    state.view.controller_diff = diff_path.relative_to(state.view.repo_root).as_posix()
    state.view.verification_paths = delta.verification_paths(state.view.scope_review_paths, state.cfg)
    if state.view.verification_paths:
        requirement = ('Changed verification files are justified by the task and do not weaken, '
                       'skip or delete existing checks: ' + ', '.join(state.view.verification_paths))
        if requirement not in state.view.required_requirements:
            state.view.required_requirements = [*state.view.required_requirements, requirement]
    # Moves that lower the quality bar (skips, removed assertions, silenced
    # checkers, stubs, loosened thresholds) must be justified by the reviewer.
    if state.cfg.execution.floor_guard.enabled:
        state.floor = floor_guard.scan(state.view.agent_baseline, current, state.view.scope_review_paths)
        (state.handoff / f'floor_guard_{attempt+1}.json').write_text(
            json.dumps(state.floor, indent=2, ensure_ascii=False), encoding='utf-8')
        state.view.required_requirements = floor_guard.replace_requirements(state.view.required_requirements, state.floor)
    # Sensitive categories (money, auth, destructive schema, size). In the
    # default report mode they never stop the run: the reviewer must rule
    # on each, and they are reported to the owner after completion.
    state.owner_flags = owner_review.evaluate( state.cfg.execution.owner_review,
        owner_review.changed(state.view.agent_baseline, current, state.view.scope_review_paths),
        state.accepted_intent['approved_sensitive'])
    (state.handoff / f'owner_review_{attempt+1}.json').write_text(
        json.dumps(state.owner_flags, indent=2, ensure_ascii=False), encoding='utf-8')
    if state.cfg.execution.owner_review.mode == 'report':
        state.view.required_requirements = owner_review.replace_requirements(
            state.view.required_requirements, state.owner_flags)


def _build_attempt(state: TaskState, attempt, reuse_candidate):
    if not reuse_candidate:
        # Never begin a cycle the remaining time cannot finish (SYS-04).
        timing.require(state.view, state.clock, [('build', 1), ('checks', 1), ('review', 1)],
                       'DEADLINE_INSUFFICIENT', 'build, check and review another candidate')
        started = __import__('time').monotonic()
        continuation = 0
        while True:
            build_name = f'build_{attempt+1}' + (f'_continuation{continuation}' if continuation else '')
            try:
                with state.run.phase(PhaseParams(name=build_name, kind='agent', owner='builder', retries=1,
                                           description='Implement or repair the bounded task in staging')) as ph:
                    state.build = agents.execute(state.view, ph.phase, AgentCall(output_type=BuildOutput,
                        prompt=state.build_prompt, previous=state.previous, plan=state.plan))
                break
            except RoleTimeout as error:
                if (not state.policy.recovery_rounds or not state.cfg.execution.resume_failed_builds
                        or not getattr(error, 'resume_safe', False)):
                    raise
                _recover(state, 'builder_role_timeout')
                state.recovered_build_phases.append(ph.phase.phase_id)
                execution_budget.ensure_remaining_sends(state.view, 1 + 1)
                continuation += 1
                state.build_prompt += ('\n\nCONTINUING INTERRUPTED BUILD: the controller retained permitted '
                                 'partial source in this staging copy after a role timeout. Continue '
                                 'the same approved plan from those actual bytes; inspect the delivered '
                                 'failed phase records and progress notes. Completion still requires '
                                 'all checks and independent review. Last interruption: ' + str(error))
            except scope.ScopeViolation as error:
                paths = list(getattr(error, 'paths', []) or [])
                # Legacy reviewers may name a needed file in the
                # blocker text instead of the structured hint.
                if (not state.policy.recovery_rounds or not state.last_review or not paths
                        or not all(p in '\n'.join(state.last_review['blocking']) for p in paths)):
                    raise
                _revise_paths(state, paths, str(error))
                state.recovered_build_phases.append(ph.phase.phase_id)
                continuation += 1
        state.clock.done('build', __import__('time').monotonic() - started)
        _record_handoff(state, state.build, build_name)
    else:
        # The saved report travels, but its prior workspace evidence does not.
        saved_build = dict(state.kept.get('last_build') or {})
        state.build = BuildOutput.model_validate({
            **saved_build, 'status': 'success', 'artifacts': [], 'artifact_declarations': [],
            'summary': (saved_build.get('summary') or 'Saved candidate') +
                       f" [kept candidate of run {state.kept['run_id']}, reviewed without a builder round]",
            'changed_files': list(state.kept['paths'])})
        _record_handoff(state, state.build, 'build_reused')


def _document(state: TaskState, attempt, reuse_candidate):
    if reuse_candidate:
        saved_documentation = state.kept['checked'].get('documentation')
        if saved_documentation is None and not state.cfg.execution.documentation.enabled:
            saved_documentation = documentation._baseline_receipt(state.view, state.plan)
        state.view.documentation_result = documentation.restore_candidate(state.view, state.plan, saved_documentation)
    else:
        state.view.documentation_result = documentation.prepare_candidate(state.view, state.plan, source_paths(state.staged), attempt=attempt + 1, on_handoff=lambda envelope, phase: _record_handoff(state, envelope, phase))
    if state.cfg.execution.documentation.enabled:
        note = ('Documentation impact: ' + state.view.documentation_result['decision'] + '. ' +
                state.view.documentation_result['reason'] + '. Inspect every proposed document and unchanged guide confirmation against actual behavior; '
                'Confirmed guide paths: ' + json.dumps([row['path'] for row in
                    state.view.documentation_result.get('confirmed_documents', [])]) + '. '
                'historical records cannot override active guides. Required documentation must be complete.')
        if note not in state.view.required_requirements:
            state.view.required_requirements.append(note)
    scope._atomic_json(state.handoff / f'documentation_{attempt+1}.json', state.view.documentation_result)


def _review_round(state: TaskState, attempt):
    # A review that cannot finish is not started; the checked candidate is
    # kept for a review-only continuation (SYS-04, SYS-10).
    timing.require(state.view, state.clock, [('review', 1)], 'CANDIDATE_CHECKED_UNREVIEWED',
                   'review this checked candidate')
    evidence_reviews = 0
    while True:
        suffix = f'_evidence{evidence_reviews}' if evidence_reviews else ''
        started = __import__('time').monotonic()
        with state.run.phase(PhaseParams(name=f'review_{attempt+1}{suffix}', kind='agent', owner='reviewer', retries=1,
                                   description='Independently review behavior and each changed file against task scope')) as ph:
            ph.log(controller_diff=state.view.controller_diff, verification_paths=state.view.verification_paths)
            state.reviewed = agents.execute(state.view, ph.phase, AgentCall(output_type=ReviewOutput,
                prompt=state.prompt, previous=state.build, gates=[gates.verdict_consistent]))
        _record_handoff(state, state.reviewed, f'review_{attempt+1}{suffix}_combined')
        state.clock.done('review', __import__('time').monotonic() - started)
        state.last_review = review_record(state.run, f'review_{attempt+1}{suffix}', state.reviewed, state.verified_source)
        state.round_number = len(state.registry.rounds) + 1
        state.registry.rule(state.reviewed.findings, state.round_number, state.reviewed.approved)
        if state.reviewed.approved or state.advisory:
            break
        # Route the rejection by what it needs (FIX-03): only a code change
        # starts a builder round.
        route = blocker_route(state.reviewed)
        if route == 'code_change':
            break
        # The task stays `reviewing`: no repair round, no builder (the project ledger
        # allows reviewing -> needs_repair -> running only).
        state.view.required_requirements = findings.carry_forward(
            state.view.required_requirements, state.reviewed.blocking, state.round_number, state.registry,
            categories=categories_of(state.reviewed))
        state.registry.save(Path(state.run.session_dir) / 'findings.json')
        evidence_packet.deliver_json(state.view, f'findings/round-{state.round_number:03d}.json',
                                     state.registry.to_dict(), producer='controller', kind='findings_registry')
        if (route == 'evidence_delivery' and evidence_reviews < state.cfg.execution.evidence_review_rounds):
            # Re-verify the delivery, publish an explicit inventory, and review the
            # unchanged candidate again; no repair round is consumed.
            evidence_reviews += 1
            timing.require(state.view, state.clock, [('review', 1)], 'CANDIDATE_CHECKED_UNREVIEWED',
                           'review this checked candidate again')
            evidence_packet.verify(state.view)
            evidence_packet.deliver_json(state.view, f'evidence_index-{state.round_number:03d}.json',
                                         evidence_packet.inventory(state.view), producer='controller',
                                         kind='evidence_inventory')
            execution_budget.ensure_remaining_sends(state.view, 1)
            continue
        draft = evidence_packet.decision_draft(state.view, state.reviewed, categories_of(state.reviewed),
                                               state.task_id or state.work_key if state.cfg.execution.resume_failed_builds
                                               else state.task_id or 'task')
        target = evidence_packet.deliver_json(state.view, f'decision_draft-{state.round_number:03d}.json', draft,
                                              producer='controller', kind='decision_draft')
        state.previous = state.reviewed
        if route == 'infrastructure_or_provider':
            raise RuntimeError('INFRA_OR_PROVIDER_BLOCKER: the independent review names an '
                               'infrastructure or provider problem; no builder round can fix it. '
                               'Candidate kept. Findings: ' + '; '.join(state.reviewed.blocking)[:1500])
        raise RuntimeError('OWNER_DECISION_REQUIRED: the remaining findings need records that do '
                           'not exist or an owner decision, not a code change. Candidate kept '
                           f'checked; a decision draft is at {target.name} in the controller '
                           'inputs. Record a decision in .kh/dispositions.json, then run '
                           '/kh-start --review-existing. Findings: ' + '; '.join(state.reviewed.blocking)[:1500])


def _accept_or_repair(state: TaskState):
    if state.advisory:
        receipt_path = advisory_receipt(state.run, state.view, state.work_key, state.kept, state.reviewed, state.verified_source,
                                        state.checked, state.owner_decisions)
        state.run.console.note(f"advisory review {'APPROVED' if state.reviewed.approved else 'NOT approved'}; "
                         f'nothing was promoted and the saved candidate is unchanged; receipt: ' f'{receipt_path}')
        state.task.log(advisory_review=str(receipt_path), approved=state.reviewed.approved)
        from adw_modules.evidence import publish_staged_evidence
        publish_staged_evidence(state.run, state.view)
        state.staged.evidence_published = True
        return True
    if state.reviewed.approved:
        state.accepted = True
        state.registry.save(Path(state.run.session_dir) / 'findings.json')
        return True
    _progress(state, 'needs_repair', blocking=state.reviewed.blocking)
    state.previous = state.reviewed
    # Later reviewers must rule on each earlier blocker with evidence, by stable ID (SYS-08).
    state.view.required_requirements = findings.carry_forward(
        state.view.required_requirements, state.reviewed.blocking, state.round_number, state.registry,
        categories=categories_of(state.reviewed))
    state.registry.save(Path(state.run.session_dir) / 'findings.json')
    evidence_packet.deliver_json(state.view, f'findings/round-{state.round_number:03d}.json', state.registry.to_dict(),
                                 producer='controller', kind='findings_registry')
    if state.review_existing:
        state.existing_rejected = True
        raise RuntimeError('REVIEW_EXISTING_REJECTED: the independent review rejected the saved '
                           'candidate; its findings are kept for the next normal run: '
                           + '; '.join(state.reviewed.blocking)[:2000])
    missing_paths = [p for p in state.reviewed.repair_paths
                     if not any(permissions._matches(p, pattern) for pattern in state.plan.allowed_paths)]
    if missing_paths:
        if not state.policy.recovery_rounds:
            raise RuntimeError('PLAN_REVISION_REQUIRED: reviewer needs paths omitted from the approved plan')
        _revise_paths(state, missing_paths, '; '.join(state.reviewed.blocking))
    return False


def _open_task(state: TaskState, prepare_context, context_state):
    # An interrupted builder is a failed phase, not yet the task's final
    # outcome. The guarded controller settles the session after recovery.
    state.run.defer_phase_failure_finish = True
    # Infrastructure boundary for every agent of this task (SYS-05); probes run
    # with the agents' own environment before any model is dispatched.
    infrastructure.configure(state.cfg, state.run.adw_id)
    state.run.owner_decisions = state.owner_decisions
    state.run.evidence_policy = dispositions.policy_record(state.cfg, state.owner_decisions)
    state.clock = timing.Clock(state.run.repo_root, state.policy.mode, record=not state.advisory)
    state.run.task_id = state.task_id
    if state.task_id and not state.advisory:
        current = project.load_task(state.run.repo_root, state.task_id)
        if task_contract(current) != state.contract:
            raise ValueError('Project task scope differs from the recorded plan')
        project.begin_task(state.run.repo_root, state.task_id, state.run.adw_id, require_plan_review=True)
        state.leased = True
    scope.capture_contract(state.run, state.contract)
    infrastructure.run_capability_checks(state.cfg, utils.operator_env(), state.run.session_dir)
    evidence_packet.deliver_json(state.run, 'policy.json', state.run.evidence_policy, producer='controller',
                                 kind='evidence_policy')
    if prepare_context:
        project_context.bootstrap_with_budget(state.run, state.decision, refresh=context_state['status'] == 'stale')
    execution_budget.start_budget(state.run, state.policy)
    if not state.review_existing:
        project_context.sync_commands(state.run.repo_root, state.cfg, repo_lock=state.run._lock)
    state.context_packet = intent.attach(
        project_context.build_context_packet(state.run.repo_root, state.prompt, state.contract.allowed_paths),
        state.accepted_intent)
    state.run.project_context_packet = state.context_packet
    state.understanding = project_context.write_understanding(state.run, state.context_packet)
    scope.capture_contract(state.run, state.contract)
    record = state.run.session_dir / 'routing.json'
    if record.exists() and scope._read_json(record) != state.decision:
        raise scope.ScopeViolation('Routing changed for an existing run; use a new run')
    scope._atomic_json(record, state.decision)
    with state.run.phase(PhaseParams(name='request', kind='engineer', owner=state.run.engineer,
                               description='Capture original task, scope and resolved role assignments')) as ph:
        ph.log(input=state.prompt, routing=state.decision, scope=state.contract.model_dump())
    state.builder = agents.resolve(state.cfg, 'builder')


def _stage_inputs(state: TaskState, owner_evidence):
    state.view = state.staged.view
    # The outer boundary is controller-owned task evidence. Individual
    # workers get narrower phase-specific evidence folders as usual.
    state.handoff = state.view.context_handoff_dir.parent.parent
    state.view.context_handoff_dir = state.handoff
    state.view.agent_output_relative = state.handoff.relative_to(state.view.repo_root).as_posix()
    state.view.required_requirements = list(state.run.required_requirements)
    state.view.agent_baseline = permissions.snapshot(state.view)
    state.view.task_baseline = state.view.agent_baseline
    state.registry = findings.Registry()
    state.view.findings_registry = state.registry
    evidence_packet.deliver_json(state.view, 'policy.json', state.run.evidence_policy, producer='controller',
                                 kind='evidence_policy')
    # Original records the owner supplies (an older harness session, say), copied
    # unchanged with their source path and hash (FIX-04, incident recovery).
    for index, path in enumerate(owner_evidence, 1):
        evidence_packet.deliver_file(state.view, path, f'owner_supplied/{index:02d}-{path.name}',
                                     producer='owner', kind='owner_supplied_original', provenance=str(path.resolve()))
    state.view.scope_contract = state.contract
    state.view.scope_review_paths = None
    state.view.record_observations = lambda envelope, phase: state.observations.append((envelope, phase))


def _plan(state: TaskState):
    planning_started = __import__('time').monotonic()
    state.plan, state.plan_approval = planning.run_reviewed_plan(state.view, state.prompt, state.contract, state.context_packet,
        max_repairs=state.decision.get('max_plan_repairs', 2), on_handoff=lambda envelope, phase: _record_handoff(state, envelope, phase),
        require_reuse=state.review_existing)
    if not state.plan_approval.reused_from:
        state.clock.done('planning', __import__('time').monotonic() - planning_started)
    planning.assert_plan_approval(state.view, state.plan, state.plan_approval, state.contract, state.context_packet)
    if state.context_packet.get('conditional_documents'):
        state.run.project_context_packet = state.view.project_context_packet
        state.understanding = project_context.write_understanding(state.run, state.view.project_context_packet)
    # The complete approved plan and its approval chain, as controller records (SYS-03).
    evidence_packet.deliver_plan(state.view, state.plan, state.plan_approval, state.run.session_dir)
    state.view.packet_identity = {'plan_digest': state.plan_approval.plan_digest,
                            'plan_content_digest': planning._content_digest(state.plan)}
    state.build_prompt = planning.build_brief(state.prompt, state.plan_approval)


def _resume(state: TaskState):
    state.work_key = resume.task_key(state.task_id, planning.digest(state.contract))
    if state.cfg.execution.resume_failed_builds:
        state.plan_content = planning._content_digest(state.plan)
        # Fingerprint of the builder-writable source, taken before kept work is applied.
        state.pristine = quality.workable_fingerprint(state.view)
        state.kept, state.info = resume.apply(state.run.repo_root, state.work_key, owner_digest=state.pristine,
                                  plan_content=state.plan_content, stage_root=state.view.repo_root,
                                  archive=not state.advisory, excluded=exclusions(state.run),
                                  allowed=lambda rel: permissions.permitted(rel, state.builder, state.cfg))
        if state.kept:
            resume.deliver_packet(state.view, state.kept, state.run.repo_root, state.work_key)
            state.last_review = state.kept.get('last_review')
            if state.kept.get('findings'):
                # Earlier findings stay requirements until a review resolves them.
                state.registry.findings.update(findings.Registry(state.kept['findings']).findings)
                state.registry.rounds.extend(findings.Registry(state.kept['findings']).rounds)
            state.view.required_requirements = state.registry.merge_requirements(state.view.required_requirements)
            state.build_prompt = state.build_prompt + '\n\n' + resume.brief(state.kept, state.info)
            state.run.console.note(f"resuming the unaccepted work of run {state.kept['run_id']} "
                             f"({len(state.kept['paths'])} paths); the builder continues it")
            state.task.log(resumed_from=state.kept['run_id'], resumed_paths=state.kept['paths'],
                     resumed_reason=state.kept['reason'])
        elif state.info:
            state.run.console.note(state.info)
            state.task.log(resume_skipped=state.info)
        if not state.advisory:
            resume.write_plan(state.run.repo_root, state.work_key, state.plan, state.info if state.kept else None)
        state.view.salvage_failed_build = True

        # An advisory review leaves the saved candidate exactly as it was.
        state.staged.resume_save = None if state.advisory else lambda reason: _resume_save(state, reason)


def _scope(state: TaskState):
    state.view.scope_contract = scope.effective_contract(state.run, state.contract, list(state.plan.allowed_paths))
    if not state.review_existing:
        documentation.validate_setup(state.view, state.plan)
    validate_light_paths(state.cfg, state.policy, state.plan.allowed_paths)
    if state.policy.mode == "light" and any((state.view.repo_root / p).is_dir() for p in state.plan.allowed_paths):
        raise ValueError("REQUIRES_FULL_MODE: light scope must name files")
    if state.review_existing:
        if not state.kept:
            raise RuntimeError('REVIEW_EXISTING_UNAVAILABLE: no kept candidate of this task matches the '
                               'current owner tree and approved plan')
        if not state.kept.get('checked'):
            raise RuntimeError('REVIEW_EXISTING_UNAVAILABLE: the kept candidate never passed the '
                               'controller checks; run the task normally')


def _accept(state: TaskState):
    if not state.advisory:
        if not state.accepted:
            raise RuntimeError('Task was not accepted within the configured repair budget; unaccepted work kept when enabled')
        # Last check evidence is used only for the unchanged source delta.
        # Reviewers are source read-only; nested permission checks enforce it.
        if quality.source_fingerprint(state.view) != state.verified_source:
            raise RuntimeError('VERIFICATION_SOURCE_CHANGED: candidate changed after successful checks')
        # Opt-in hold mode only: owner-held categories stop promotion here, judged
        # on the verified, reviewed bytes (unchanged since the checks, above).
        # The failure path still publishes the candidate evidence and diff.
        if state.cfg.execution.owner_review.mode == 'hold' and state.owner_flags:
            state.task.log(owner_review_holds=state.owner_flags)
            raise RuntimeError(owner_review.hold_message(state.owner_flags, state.view.controller_diff))
        from adw_modules.evidence import publish_staged_evidence
        publish_staged_evidence(state.run, state.view)
        state.staged.evidence_published = True
        promoted_paths = list(state.view.scope_review_paths)
        verified_entries = permissions.snapshot(state.view)
        validate_light_paths(state.cfg, state.policy, promoted_paths)
        execution_budget.remaining_timeout(state.run, state.policy.deadline_seconds)
        docs_projection = documentation.context_projection(state.view, state.view.documentation_result)
        state.staged.promote(state.build)
        state.source_promoted = True
        docs_receipt = documentation.finish(state.run, state.view.documentation_result, docs_projection)
        if state.cfg.execution.resume_failed_builds:
            resume.clear(state.run.repo_root, state.work_key)
        if state.task_id:
            receipt = _completion_receipt(state, promoted_paths, verified_entries, docs_receipt)
            scope._atomic_json(project.receipt_path(state.run.repo_root, state.task_id, state.run.adw_id), receipt)
            project.complete_task(state.run.repo_root, state.task_id, state.run.adw_id, receipt)
            state.task_completed = True
        for envelope, phase in state.observations:
            rebased = type(envelope).model_validate_json(envelope.model_dump_json().replace(
                str(state.view.repo_root), str(state.run.repo_root)))
            state.run.record_observations(rebased, phase)
        state.run.required_requirements = list(state.view.required_requirements)
        state.task.log(promoted=promoted_paths, review_approved=True, checks_passed=True,
                 owner_attention=state.owner_flags, floor_guard=state.floor)
        if state.owner_flags:
            state.run.console.note('For the owner after completion: ' + '; '.join(
                f"{f['category']}: {f['why']}" for f in state.owner_flags))


def _execute_task(prompt, contract, prepared, adw_id=None, task_id=None, mode="full", repo_lock=None,
                  review_existing=False, advisory=False):
    """A failed task discards all candidate source; trace stays controller-owned."""
    (cfg, decision, policy, accepted_intent, context_state, prepare_context, owner_evidence,
     builder_preflight, required_roles) = prepared
    decision = {**decision, "max_repairs": policy.max_repairs, "max_plan_repairs": policy.max_plan_repairs}
    if builder_preflight['blocked']:
        from adw_modules.runtime_compatibility import RuntimeCompatibilityError
        raise RuntimeCompatibilityError(builder_preflight)
    if not cfg.defaults.required_quality_checks:
        raise ValueError('Guarded startup requires at least one real configured quality check')
    # Every preflight passed: this is the intent's first real use, so an
    # acceptance without an explicit digest is bound to the text loaded above.
    accepted_intent = intent.bind(Path.cwd(), accepted_intent)
    command_classes = [c.name for c in cfg.execution.command_policy.classes]
    owner_decisions = dispositions.load(Path.cwd(), task_id or resume.task_key(None, planning.digest(contract)),
                                        command_classes)
    run = session.ensure(cfg, adw_id, repo_lock=repo_lock, fresh=True)
    state = TaskState(run, cfg, decision, contract, prompt, task_id, review_existing,
                      advisory, policy, accepted_intent, owner_decisions)



    try:
        _open_task(state, prepare_context, context_state)
        with state.run.phase(PhaseParams(name='guarded_task', kind='code', owner='controller',
                                   description='Keep the complete task staged until checks and scope review pass')) as state.task:
            with durable_stage(state.run, state.task.phase, state.builder) as state.staged:
                _stage_inputs(state, owner_evidence)
                _plan(state)
                _resume(state)
                _scope(state)

                state.window_checkpoint = _checkpoint(state)



                state.window_size = state.decision['max_repairs'] + 1
                state.window_end = state.window_size
                for attempt in range(state.window_size * (state.policy.recovery_rounds + 1)):
                    if attempt == state.window_end:
                        if not state.policy.recovery_rounds:
                            break
                        _recover(state, 'repair_window', require_progress=True)
                        state.window_end += state.window_size
                    if attempt:
                        execution_budget.record_repair(state.view, 'code')
                        _progress(state, 'running', attempt=attempt + 1)
                    reuse_candidate = state.review_existing and attempt == 0
                    execution_budget.ensure_remaining_sends(state.view, (0 if reuse_candidate else 1) + 1)
                    _build_attempt(state, attempt, reuse_candidate)
                    _document(state, attempt, reuse_candidate)
                    if not _run_checks(state, attempt, reuse_candidate):
                        continue
                    _measure_delta(state, attempt)
                    _review_round(state, attempt)
                    if _accept_or_repair(state):
                        break
                _accept(state)
        infrastructure.reset()
        if state.advisory:
            finished = state.run.finish(accepted=state.reviewed.approved, reason='' if state.reviewed.approved else 'ADVISORY_REVIEW_REJECTED')
            return finished if state.reviewed.approved else 2
        return state.run.finish(recovered_phases=state.recovered_build_phases)
    except BaseException as error:
        execution_budget.record_stop(state.run, str(error) or type(error).__name__)
        infrastructure.reset()
        try:
            if state.leased and not state.task_completed:
                reason = str(error) or type(error).__name__
                if state.source_promoted:
                    reason = 'Source was promoted; completion recording needs reconciliation: ' + reason
                try:
                    project.fail_task(state.run.repo_root, state.task_id, state.run.adw_id, f'{reason} [{spend_line(state.run)}]',
                                      attempt_summary={'cost': spend(state.run), 'execution': execution_budget.summary(state.run),
                                                       'handoffs': list(state.handoffs)})
                except Exception as ledger_error:
                    state.run.console.note(f'Could not record blocked task; inspect/recover ledger: {ledger_error}')
            state.run._finish_trace(False)
            state.run.console.session_finished(False, state.run.tokens, state.run.cost, state.cfg.observability.db, state.run.cost_known)
        finally:
            session.restore_signal_handlers(state.run)
            if state.run._lock: state.run._lock.close()
        raise


def main(prompt=None, config='adws/adw_kh_config/kh.config.yaml', profile=None,
         scope_file=None, dry_run=False, save_profile=False, adw_id=None, task_id=None, mode="full", repo_lock=None,
         use=None, review_existing=False, advisory=False, evidence=None, refresh_context=False):
    project.require_sequential(Path.cwd())
    planned_task = project.load_task(Path.cwd(), task_id) if task_id else None
    if planned_task:
        if scope_file:
            raise ValueError('--task uses recorded project scope; do not combine with --scope-file')
        if prompt is not None and utils.resolve_prompt(prompt) != planned_task['request']:
            raise ValueError('--task request must match the recorded plan')
        prompt = planned_task['request']
    elif prompt is not None:
        prompt = utils.resolve_prompt(prompt)
    else:
        raise ValueError('Supply a task prompt or --task ID')
    if not prompt.strip(): raise ValueError('A nonempty task is required')
    accepted_intent = intent.load(Path.cwd(), prompt)
    cfg = agents.load_config(config)
    context_state, prepare_context = context_preflight(review_existing, refresh_context)
    roles = task_roles(cfg, prepare_context, review_existing)
    # The owner's per-run answers to "which model and effort?" (never persisted).
    run_choices = routing.run_choices(cfg, use)
    if run_choices and save_profile:
        raise ValueError('--save-profile saves a named profile, not per-run choices; keep these with '
                         '/kh-setup-models --set ROLE=RUNTIME:MODEL:EFFORT --apply')
    resolved, decision = routing.resolve(cfg, Path.cwd(), profile, required_roles=roles, overrides=run_choices)
    contract = task_contract(planned_task) if planned_task else scope.load_contract(scope_file, prompt) if scope_file else scope.ScopeContract(
        original_request=prompt, deliverables=[prompt],
        non_goals=['Unrelated feature additions or refactors', 'Unrequested external publication or deployment'])
    if not contract.review_required:
        raise ValueError('Guarded startup always requires independent review')
    prepared = preflight(prompt, resolved, decision, contract, mode, review_existing, advisory,
                         evidence, refresh_context, accepted_intent=accepted_intent,
                         context=(context_state, prepare_context), validate_agents=not dry_run)
    (resolved, decision, policy, accepted_intent, context_state, prepare_context, _,
     builder_preflight, _) = prepared
    decision['required_checks'] = list(resolved.defaults.required_quality_checks)
    for role in ('planner', 'plan_reviewer', 'reviewer'):
        agent = agents.resolve(resolved, role)
        # Native read-only Codex cannot write required per-phase artifacts; use
        # explicit workspace-write sandbox plus controller-enforced source [] .
        if agent.coding_agent == 'delegate_codex' and agent.delegate_sandbox == 'read-only':
            raise ValueError(f'{role}: required evidence needs explicit workspace-write sandbox; source writes remain []')
    preview = {**decision, "execution": policy.model_dump(),
               'builder_test_preflight': builder_preflight,
               'review_existing': bool(review_existing), 'advisory': bool(advisory),
               'refresh_context': bool(refresh_context and context_state['status'] == 'stale'),
               'owner_supplied_evidence': [str(Path(p)) for p in evidence or ()],
               'timing': timing.preflight(Path.cwd(), resolved, policy, 1,
                                          planning_needed=not review_existing),
               "bootstrap_execution": resolve_bootstrap_policy(resolved, decision).model_dump() if prepare_context else None, 'task_id': task_id, 'scope': contract.model_dump(),
               'missing_checks': [n for n in resolved.defaults.required_quality_checks
                                  if not resolved.defaults.quality_checks.get(n)],
               'context': {k: context_state.get(k) for k in ('status', 'digest', 'conflicts')},
               'workflow': 'context/bootstrap -> plan/review/revise -> build/check/review'}
    print(json.dumps(preview, indent=2, ensure_ascii=False))
    if builder_preflight['blocked']:
        from adw_modules.runtime_compatibility import RuntimeCompatibilityError
        raise RuntimeCompatibilityError(builder_preflight)
    if dry_run: return 0
    if preview['missing_checks']:
        raise RuntimeError('NOT_CONFIGURED: ' + ', '.join(preview['missing_checks']))
    if save_profile: routing.save_preference(Path.cwd(), decision['profile'], cfg)
    return _execute_task(prompt, contract, prepared, adw_id, task_id, mode, repo_lock,
                         review_existing, advisory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prompt', nargs='?')
    parser.add_argument('--task', dest='task_id', help='Execute an existing project task with recorded scope')
    parser.add_argument('--config', default=os.environ.get('KH_CONFIG', 'adws/adw_kh_config/kh.config.yaml'))
    parser.add_argument('--profile')
    parser.add_argument('--mode', choices=('full', 'light'), default='full')
    parser.add_argument('--scope-file')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--save-profile', action='store_true')
    parser.add_argument('--adw-id')
    parser.add_argument('--use', action='append', metavar='ROLE=RUNTIME:MODEL:EFFORT',
                        help='Model and effort for one role in this run only (repeatable)')
    parser.add_argument('--review-existing', action='store_true',
                        help="Review this task's kept, checked candidate without a builder round; "
                             'promotion still needs a real approval')
    parser.add_argument('--advisory', action='store_true',
                        help='With --review-existing: review only; never promote, never change task state, '
                             'leave the saved candidate unchanged; writes a review receipt')
    parser.add_argument('--evidence', action='append', metavar='PATH',
                        help='An original record to deliver unchanged to every role (repeatable), e.g. an '
                             'older harness session log')
    parser.add_argument('--refresh-context', action='store_true',
                        help='Independently review and refresh stale project context before planning; '
                             'cannot accompany --review-existing')
    try:
        raise SystemExit(main(**vars(parser.parse_args())))
    except (ValueError, RuntimeError, OSError) as error:
        print(f'kh-start: {error}', file=sys.stderr)
        raise SystemExit(1)
