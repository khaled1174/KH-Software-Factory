"""Bounded, independently reviewed task planning; only the controller approves."""
from __future__ import annotations
from .utils import digest
from . import execution_budget
from .execution_policy import validate_light_paths

import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from . import agents, gates, quality, scope, documentation
from . import conditional_docs as cd, project_context as pc
from .documentation import DocumentationPlan
from .data_types import AgentCall, EnvelopeBase, GateReport, PhaseParams, PlanOutput


class PlanningBlocked(RuntimeError):
    pass


class PlanStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    validation: list[str] = Field(min_length=1)


class FindingResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    finding_id: str
    disposition: Literal['fixed', 'disputed', 'needs_owner']
    explanation: str = Field(min_length=1)
    reference: str = Field(min_length=1)


class ReviewedPlanOutput(PlanOutput):
    model_config = ConfigDict(extra='forbid')
    documentation: DocumentationPlan | None = None
    context_selection: list[cd.ReadDecision] | None = Field(default=None, max_length=256)

    @model_serializer(mode="wrap")
    def compatible_dump(self, handler):
        data = handler(self)
        if self.context_selection is None:
            data.pop("context_selection", None)
        return data
    steps: list[PlanStep] = Field(min_length=1)
    requirement_coverage: dict[str, list[str]]
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    required_checks: list[Literal['test', 'lint', 'typecheck', 'build']]
    finding_responses: list[FindingResponse] = Field(default_factory=list)


class PlanFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1)
    severity: Literal['blocking', 'major', 'minor']
    requirement_id: str = ''
    step_id: str = ''
    evidence: str = Field(min_length=1)
    correction: str = Field(min_length=1)


class PlanReviewOutput(EnvelopeBase):
    model_config = ConfigDict(extra='forbid')
    plan_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    version: int = Field(ge=1)
    decision: Literal['approve', 'revise', 'needs_owner']
    findings: list[PlanFinding] = Field(default_factory=list)
    resolved_finding_ids: list[str] = Field(default_factory=list)
    light_eligible: bool | None = None
    light_eligibility_reason: str = ''


class PlanApproval(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal[1] = 1
    run_id: str
    task_id: str | None = None
    version: int
    plan_digest: str
    request_digest: str
    scope_digest: str
    context_digest: str
    roles_digest: str
    baseline_digest: str
    review_digest: str
    # Findings of the final review round: they do not reopen planning. The
    # builder must apply them and the code reviewer must rule on each.
    final_notes: list[dict] = Field(default_factory=list)
    # Execution mode the plan was approved under (full or light), and the run
    # whose approval this one re-issues when an unchanged task is run again.
    mode: str = ''
    reused_from: str = ''
    # The earlier approval's plan digest; this run's plan differs from it only
    # in where its evidence files live.
    reused_plan_digest: str = ''
    # Fingerprint of the builder-writable source only (quality.workable_fingerprint):
    # what a later run compares to reuse this approval after config-only changes.
    source_digest: str = ''
    parent_approval_digest: str = ''
    parent_approval_version: int = 0
    revision_reason: str = ''
    revision_paths: list[str] = Field(default_factory=list)
    revision_candidate_digest: str = ''


def requirements(contract):
    return {f'R{i}': text for i, text in enumerate(contract.requirements(), 1)}


def validate_plan(plan, contract, required_checks=(), required_test_commands=(), *, require_documentation=False, run=None):
    """Validate all frozen requirements, path containment and ordered dependencies."""
    if plan.status != 'success' or not plan.summary.strip():
        raise PlanningBlocked('Plan must report success with a substantive summary')
    scope.plan_boundary(contract, plan.allowed_paths)
    if not plan.acceptance_criteria or any(not s.strip() for s in plan.acceptance_criteria):
        raise PlanningBlocked('Plan acceptance criteria must be nonempty')
    expected = set(requirements(contract))
    if set(plan.requirement_coverage) != expected:
        raise PlanningBlocked('Plan must cover exactly all frozen requirement IDs')
    ids = [step.id for step in plan.steps]
    if len(set(ids)) != len(ids):
        raise PlanningBlocked('Duplicate plan step IDs')
    seen = set()
    for step in plan.steps:
        if (not step.action.strip() or any(not s.strip() for s in step.validation)
                or not set(step.depends_on) <= seen):
            raise PlanningBlocked('Plan steps require concrete validation and earlier dependencies')
        if not set(step.requirement_ids) <= expected:
            raise PlanningBlocked('Unknown requirement referenced by step')
        scope.plan_boundary(contract.model_copy(update={'allowed_paths': plan.allowed_paths}), step.paths)
        seen.add(step.id)
    for req in plan.requirement_coverage:
        actual = {step.id for step in plan.steps if req in step.requirement_ids}
        if not actual:
            raise PlanningBlocked(f'Requirement {req} is covered by no step: add its ID to the requirement_ids '
                                  'of the step(s) that implement it')
    if not set([*contract.required_checks, *required_checks]) <= set(plan.required_checks):
        raise PlanningBlocked('Plan omitted required quality checks')
    if not set(required_test_commands) <= set(plan.required_test_commands):
        raise PlanningBlocked('Plan omitted required native test commands')
    if run is not None and plan.required_test_commands:
        from . import runtime_compatibility
        builder = next((agent for agent in run.cfg.agents if agent.name == 'builder'), None)
        if builder is None:
            raise PlanningBlocked('Required native test commands need a configured builder')
        report = runtime_compatibility.structural(builder, run.cfg,
            required_test_commands=plan.required_test_commands, run_id=getattr(run, 'adw_id', None))
        if report['blocked']:
            failures = [f"{row['command']!r}: {'; '.join(row['issues'])}"
                        for row in report['native_test_commands'] if row['issues']]
            if not failures:
                failures = [f"{row['capability']}: {row['reason']}" for row in report['blocked']]
            raise PlanningBlocked('Required native test commands are incompatible with builder: '
                                  + '; '.join(failures))
    try:
        documentation.validate_documentation_plan(plan, contract, required=require_documentation)
    except ValueError as error:
        raise PlanningBlocked(str(error)) from error


def derive_coverage(plan):
    """The requirement->steps map computed from the steps themselves.

    The plan states each step's requirement_ids AND a requirement_coverage map
    that must mirror them exactly. That is one fact written twice, and a
    revision must not leave a stale map. The controller derives coverage
    from the steps the builder executes. What still fails a plan: a
    requirement no step covers, a step naming an unknown requirement, missing
    or reordered dependencies (validate_plan).
    Returns (plan with derived coverage, the planner's differing map or None).
    """
    derived = {req: [step.id for step in plan.steps if req in step.requirement_ids]
               for req in sorted(set(plan.requirement_coverage) |
                                 {r for step in plan.steps for r in step.requirement_ids})}
    stated = {req: list(ids) for req, ids in plan.requirement_coverage.items()}
    if {k: sorted(v) for k, v in stated.items()} == {k: sorted(v) for k, v in derived.items()}:
        return plan, None
    return plan.model_copy(update={'requirement_coverage': derived}), stated


def with_contract_requirements(plan, contract):
    """Every frozen requirement appears in the map, so an uncovered one is named, not silently absent."""
    expected = set(requirements(contract))
    missing = {req: [] for req in sorted(expected - set(plan.requirement_coverage))}
    if not missing:
        return plan
    return plan.model_copy(update={'requirement_coverage': {**plan.requirement_coverage, **missing}})


def normalize_plan(plan, contract):
    plan, stated = derive_coverage(with_contract_requirements(plan, contract))
    return plan, stated


def structure_gate(contract, required_checks=(), previous_ids=frozenset(), previous_test_commands=frozenset()):
    """A plan the controller can already refute, sent back to the planner as a correction.

    The last plan revision has no later round, so a mechanical inconsistency in
    it (coverage that names a step that does not exist, a dropped step ID) would
    end the run. The planner is still in its session and can fix exactly what is
    named; the plan-review rounds stay reserved for judgement.
    """
    def plan_structure(envelope, run):
        report = GateReport()
        try:
            envelope, _ = normalize_plan(envelope, contract)
            validate_plan(envelope, contract, required_checks, run.cfg.defaults.required_test_commands,
                          require_documentation=documentation.required(run), run=run)
            lost = set(previous_ids) - {s.id for s in envelope.steps}
            if lost:
                raise PlanningBlocked(f'Revisions must preserve existing step IDs; missing {sorted(lost)}')
            if not set(previous_test_commands) <= set(envelope.required_test_commands):
                raise PlanningBlocked('Revisions must preserve required native test commands')
            plan_structure.validated.add(digest(envelope))
            report.check('plan structure', True, 'coverage, references, dependencies and checks agree')
        except (PlanningBlocked, scope.ScopeViolation, ValueError) as error:
            report.check('plan structure', False, str(error))
        return report
    plan_structure.validated = set()
    return plan_structure


def conditional_contract(packet):
    return {'required': bool(packet.get('conditional_documents')),
            'instructions': 'Evaluate every conditional_documents entry against this task using natural-language judgement. '
                            'Read the guide with existing read tools if ANY read_when condition matches. '
                            'Return context_selection covering each path exactly once: path, document_digest, decision read|skip, '
                            'condition_ids (matching IDs for read; empty for skip), task-specific reason. '
                            'Do not guess when unable to inspect a selected guide: report unresolved. '
                            'Paths/topics cannot activate conditional guides. Mandatory instructions always apply. '
                            'The reviewer must verify both selection and plan using the delivered selected guides. '
                            'Conditional plans require explicit approval without substantive findings before building.'}


def selected_context(run, plan, packet):
    if not packet.get('conditional_documents'):
        cd.validate_selection(packet, plan.context_selection)
        return packet
    return pc.resolve_conditional_context(run.repo_root, packet, plan.context_selection,
                                          baseline=getattr(run, 'task_baseline', None))


def _record_selection(run, packet):
    if 'conditional_selection' in packet:
        _record(run, 'context-selection.json', packet['conditional_selection'])


def install_context(run, packet):
    if packet.get('conditional_documents'):
        run.project_context_packet = packet


def _roles(run):
    return digest({'roles': [agents.resolve(run.cfg, role).model_dump(mode='json')
                   for role in ('planner', 'plan_reviewer')],
                   'task_kind': run.cfg.execution.task_kind,
                   'documentation_enabled': run.cfg.execution.documentation.enabled})


def _native_test_contract(run):
    """Expose existing permissions to planning; a plan cannot create grants."""
    from . import runtime_compatibility
    builder = next((agent for agent in run.cfg.agents if agent.name == 'builder'), None)
    if builder is None:
        return {'runtime': None, 'required_test_commands': list(run.cfg.defaults.required_test_commands),
                'capture_commands': [], 'native_permissions': 'unverified', 'builder_status': 'not_configured'}
    report = runtime_compatibility.structural(builder, run.cfg, run_id=getattr(run, 'adw_id', None))
    return {'runtime': builder.coding_agent,
            'builder_tools': builder.tools, 'builder_status': report['status'],
            'required_test_commands': list(run.cfg.defaults.required_test_commands),
            'capture_commands': [row['command'] for row in report['native_test_commands']
                                 if row['exact_grant'] and row['passed']],
            'native_permissions': 'unverified'}


def _content_digest(plan):
    # Feedback wording and evidence filenames are not progress on the plan.
    return digest(plan.model_dump(exclude={'artifacts', 'finding_responses', 'observations',
                                          'notes_for_next_agent', 'commit_message', 'summary'}))


def _record(run, name, value):
    scope._atomic_json(Path(run.session_dir) / 'planning' / name, value)


def approval_path(run):
    return Path(run.session_dir) / "planning" / "approval.json"


def _record_immutable(run, name, value):
    path = Path(run.session_dir) / 'planning' / name
    if path.exists() or path.is_symlink():
        if scope._read_json(path) != value:
            raise PlanningBlocked(f'Planning history cannot be replaced: {name}')
        return
    _record(run, name, value)


def _revision_lineage(approval, directory):
    """Validated immutable parent records, newest first, for reuse and delivery."""
    parents = []
    while approval.parent_approval_digest:
        version = approval.parent_approval_version
        if not 1 <= version < approval.version:
            raise PlanningBlocked('Plan revision approval lineage is cyclic or unordered')
        parent = scope._read_json(Path(directory) / f'approval-{version}.json')
        parsed = PlanApproval.model_validate(parent)
        plan = scope._read_json(Path(directory) / f'plan-{version}.json')
        review = scope._read_json(Path(directory) / f'review-{version}.json')
        if (parsed.version != version or digest(parent) != approval.parent_approval_digest
                or digest(plan) != parsed.plan_digest or digest(review) != parsed.review_digest):
            raise PlanningBlocked('Plan revision parent records differ from the approval lineage')
        parents.append((version, {'approval': parent, 'plan': plan, 'review': review}))
        approval = parsed
    return parents


def revision_paths(run, old_plan, owner_contract, required_paths):
    """Check a requested plan amendment before consuming any recovery or send."""
    from . import permissions
    if not isinstance(required_paths, list) or not required_paths:
        raise PlanningBlocked('Plan revision requires concrete additional paths')
    paths = []
    for path in required_paths:
        if (not isinstance(path, str) or any(char in path for char in '*?[]') or path.endswith('/')):
            raise PlanningBlocked('Plan revision paths must name concrete files')
        try:
            scope._relative_pattern(path)
        except ValueError as error:
            raise PlanningBlocked(str(error)) from error
        if not any(permissions._matches(path, pattern) for pattern in old_plan.allowed_paths):
            paths.append(path)
    paths = list(dict.fromkeys(paths))
    if not paths:
        raise PlanningBlocked('Plan revision adds no path outside the approved plan')
    boundary = owner_contract if owner_contract.allowed_paths is not None else owner_contract.model_copy(
        update={'allowed_paths': old_plan.allowed_paths})
    scope.plan_boundary(boundary, paths)
    builder = agents.resolve(run.cfg, 'builder')
    if any(not permissions.permitted(path, builder, run.cfg) for path in paths):
        raise PlanningBlocked('Plan revision cannot authorize protected or role-prohibited paths')
    return paths


def _planner_brief(run, contract, context_packet, **values):
    previous = values.pop('previous_plan', None)
    return {'conditional_context_contract': conditional_contract(context_packet), 'execution_mode': _mode(run),
            'request': contract.original_request, 'scope': contract.model_dump(), 'context': context_packet,
            'requirements': requirements(contract), 'native_test_contract': _native_test_contract(run),
            'documentation_contract': documentation.planning_contract(run), 'task_kind': run.cfg.execution.task_kind,
            'previous_plan': previous.model_dump() if previous is not None else None,
            'previous_plan_digest': digest(previous) if previous is not None else None, **values}


def _review_brief(brief, context, plan, **values):
    return {**brief, 'context': context, 'plan': plan.model_dump(), 'plan_digest': digest(plan), **values}


def _assert_review_binding(review, version, plan, message):
    if review.status != 'success' or review.version != version or review.plan_digest != digest(plan):
        raise PlanningBlocked(message)


def _approval(run, plan, review, **values):
    return PlanApproval(run_id=run.adw_id, task_id=getattr(run, 'task_id', None), version=review.version,
                        plan_digest=digest(plan), review_digest=digest(review), mode=_mode(run), **values)


def revise_approved_plan(run, old_plan, old_approval, owner_contract, context_packet, *,
                         required_paths, reason, on_handoff=None):
    """Review a bounded path addition against the unchanged rejected candidate.

    This controller API never activates writes. It retains the old approval,
    issues a new exact-version approval only on an explicit independent approve,
    and leaves candidate bytes and the owner's accepted contract unchanged.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise PlanningBlocked('Plan revision requires a concrete rejection reason')
    paths = revision_paths(run, old_plan, owner_contract, required_paths)
    for role in ('planner', 'plan_reviewer'):
        if agents.resolve(run.cfg, role).writes != []:
            raise PlanningBlocked('Planning roles must have writes=[]')
    try:
        saved = scope._read_json(approval_path(run))
        prior = PlanApproval.model_validate(saved)
        old_review = scope._read_json(Path(run.session_dir) / 'planning' / f'review-{prior.version}.json')
        saved_plan = scope._read_json(Path(run.session_dir) / 'planning' / f'plan-{prior.version}.json')
        expected = {'run_id': run.adw_id, 'task_id': getattr(run, 'task_id', None),
                    'request_digest': digest(owner_contract.original_request), 'scope_digest': digest(owner_contract),
                    'context_digest': digest(context_packet), 'roles_digest': _roles(run), 'plan_digest': digest(old_plan)}
        if (prior != old_approval or any(getattr(prior, key) != value for key, value in expected.items())
                or digest(saved_plan) != prior.plan_digest or digest(old_review) != prior.review_digest):
            raise PlanningBlocked('Prior plan approval is missing, stale or changed')
    except (OSError, ValueError, scope.ScopeViolation) as error:
        raise PlanningBlocked('Prior plan approval is missing, stale or changed') from error
    validate_plan(old_plan, owner_contract, run.cfg.defaults.required_quality_checks,
                  run.cfg.defaults.required_test_commands, require_documentation=documentation.required(run), run=run)
    baseline = quality.source_fingerprint(run)
    frozen = (digest(owner_contract), digest(context_packet), _roles(run))
    versions = [int(match[1]) for path in (Path(run.session_dir) / 'planning').glob('*.json')
                if (match := re.fullmatch(r'(?:plan|review|approval)-(\d+)\.json', path.name))]
    version = max([old_approval.version, *versions]) + 1
    execution_budget.ensure_remaining_sends(run, 2)
    execution_budget.record_repair(run, 'plan')
    install_context(run, context_packet)
    brief = _planner_brief(run, owner_contract, context_packet,
        required_checks=list(dict.fromkeys([*run.cfg.defaults.required_quality_checks,
                        *old_plan.required_checks])),
        carried_requirements=list(getattr(run, 'required_requirements', [])), version=version, previous_plan=old_plan,
        required_paths=paths, reason=reason, candidate_digest=baseline,
        instructions='Return a full revised plan. Preserve every old step ID, acceptance criterion and '
                     'required check. Retain the old path boundary and add only the requested concrete '
                     'files inside the owner scope. Address the independently rejected candidate; '
                     'retain required_test_commands and declare any additional native tests as exact commands. '
                     'The native_test_contract describes configured grants, not permission to add commands. '
                     'planning grants no permission to edit source.')
    with run.phase(PhaseParams(name=f'replan_{version}', kind='agent', owner='planner', retries=1,
                              description='Revise the approved plan for a rejected candidate')) as ph:
        plan = agents.execute(run, ph.phase, AgentCall(output_type=ReviewedPlanOutput,
            prompt=json.dumps(brief, ensure_ascii=False), gates=[gates.artifacts_exist, gates.files_non_empty]))
    plan, _ = normalize_plan(plan, owner_contract)
    _record_immutable(run, f'plan-{version}.json', plan.model_dump())
    if on_handoff:
        on_handoff(plan, f'replan_{version}')
    validate_plan(plan, owner_contract, [*run.cfg.defaults.required_quality_checks, *old_plan.required_checks],
                  run.cfg.defaults.required_test_commands, require_documentation=documentation.required(run), run=run)
    if not {step.id for step in old_plan.steps} <= {step.id for step in plan.steps}:
        raise PlanningBlocked('Plan revision must preserve previous step IDs')
    if not set(old_plan.acceptance_criteria) <= set(plan.acceptance_criteria):
        raise PlanningBlocked('Plan revision must preserve previous acceptance criteria')
    if not set(old_plan.required_test_commands) <= set(plan.required_test_commands):
        raise PlanningBlocked('Plan revision must preserve required native test commands')
    try:
        documentation.validate_documentation_plan(plan, owner_contract, previous=old_plan.documentation)
    except ValueError as error:
        raise PlanningBlocked(str(error)) from error
    minimum = [*old_plan.allowed_paths, *paths]
    scope.plan_boundary(owner_contract.model_copy(update={'allowed_paths': minimum}), plan.allowed_paths)
    scope.plan_boundary(owner_contract.model_copy(update={'allowed_paths': plan.allowed_paths}), minimum)
    resolved_context = selected_context(run, plan, context_packet)
    install_context(run, resolved_context)
    review_brief = _review_brief(brief, resolved_context, plan,
        previous_findings=list(old_approval.final_notes),
        instructions='Review this exact version independently. Explicitly approve only if '
                     'the additional paths are needed for the rejected candidate, every owner '
                     'and carried requirement survives, and the old checks, steps and boundary '
                     'are preserved. A revise or needs_owner verdict cannot authorize execution.')
    with run.phase(PhaseParams(name=f'replan_review_{version}', kind='agent', owner='plan_reviewer', retries=1,
                              description='Independently approve or reject this plan revision')) as ph:
        review = agents.execute(run, ph.phase, AgentCall(output_type=PlanReviewOutput,
            prompt=json.dumps(review_brief, ensure_ascii=False), gates=[gates.artifacts_exist, gates.files_non_empty]))
    _record_immutable(run, f'review-{version}.json', review.model_dump())
    if on_handoff:
        on_handoff(review, f'replan_review_{version}')
    _assert_review_binding(review, version, plan, 'Plan revision review returned a stale digest or version')
    if review.decision != 'approve' or any(f.severity in ('blocking', 'major') for f in review.findings):
        raise PlanningBlocked('Plan revision requires explicit approval without substantive findings')
    if (quality.source_fingerprint(run) != baseline
            or (digest(owner_contract), digest(context_packet), _roles(run)) != frozen):
        raise PlanningBlocked('Candidate or planning inputs changed during plan revision')
    policy = getattr(run, 'execution_policy', None)
    if policy is not None and policy.mode == 'light':
        validate_light_paths(run.cfg, policy, plan.allowed_paths)
        if review.light_eligible is not True or not review.light_eligibility_reason.strip():
            raise PlanningBlocked('REQUIRES_FULL_MODE: revision reviewer did not establish light eligibility')
    approval = _approval(run, plan, review,
        request_digest=digest(owner_contract.original_request), scope_digest=frozen[0], context_digest=frozen[1],
        roles_digest=frozen[2], baseline_digest=baseline, final_notes=list(old_approval.final_notes),
        source_digest=old_approval.source_digest, parent_approval_digest=digest(saved),
        parent_approval_version=old_approval.version, revision_reason=reason, revision_paths=paths,
        revision_candidate_digest=baseline)
    execution_budget.remaining_timeout(run, 86400)
    _record_immutable(run, f'approval-{old_approval.version}.json', saved)
    _record_immutable(run, f'approval-{version}.json', approval.model_dump())
    scope.add_plan_requirements(run, [*plan.acceptance_criteria, *final_note_requirements(approval.final_notes)])
    _record_selection(run, resolved_context)
    _record(run, 'approval.json', approval.model_dump())
    with run.phase(PhaseParams(name=f'plan_reapproved_{version}', kind='code', owner='controller',
                              description='Record approved plan revision lineage')) as ph:
        ph.log(version=version, parent_approval_digest=approval.parent_approval_digest,
               plan_digest=approval.plan_digest, required_paths=paths, candidate_digest=baseline)
    return plan, approval


def run_reviewed_plan(run, request, contract, context_packet, *, max_repairs, on_handoff=None, require_reuse=False):
    if type(max_repairs) is not int or not 0 <= max_repairs <= 5:
        raise PlanningBlocked('max_plan_repairs must be an integer in 0..5')
    if request != contract.original_request:
        raise PlanningBlocked('Request differs from frozen contract')
    for role in ('planner', 'plan_reviewer'):
        if agents.resolve(run.cfg, role).writes != []:
            raise PlanningBlocked('Planning roles must have writes=[]')
    baseline = quality.source_fingerprint(run)
    frozen_scope = digest(contract)
    frozen_context = digest(context_packet)
    frozen_roles = _roles(run)
    reused = reuse_approved_plan(run, request, contract, context_packet, baseline=baseline,
                                 roles=frozen_roles, max_repairs=max_repairs, on_handoff=on_handoff)
    if reused is not None:
        return reused
    if require_reuse:
        # A review-only continuation reviews the candidate built under an
        # approved plan; it never plans anew (SYS-10).
        raise PlanningBlocked('REVIEW_EXISTING_REQUIRES_APPROVED_PLAN: no earlier approval of this exact task, '
                              'scope, context, planning roles and source can be reused; run the task normally')
    prior = None
    last_plan = None
    unresolved = {}
    seen = set()
    previous_ids = set()
    previous_test_commands = set(run.cfg.defaults.required_test_commands)
    # At most two review rounds: the plan, its review, one revision, and a
    # final review whose findings become binding notes for the build instead
    # of a third round. max_plan_repairs 0 keeps a single, final review.
    rounds = 1 + min(max_repairs, 1)
    checks = run.cfg.defaults.required_quality_checks
    policy = getattr(run, "execution_policy", None)
    light = policy is not None and policy.mode == "light"
    for version in range(1, rounds + 1):
        final = version == rounds
        if version > 1:
            execution_budget.record_repair(run, "plan")
        execution_budget.ensure_remaining_sends(run, 4 if policy is not None and policy.mode != "bootstrap" else 2)
        install_context(run, context_packet)
        brief = _planner_brief(run, contract, context_packet,
            execution_mode='light' if light else 'full', version=version, required_checks=checks,
            retained_test_commands=sorted(previous_test_commands), previous_plan=last_plan,
            open_finding_ids=sorted(unresolved),
            instructions='Return the full ReviewedPlanOutput. Preserve existing step IDs; give one finding_response for each id in open_finding_ids. '
                         'Order steps so each one uses only what the current tree or an earlier step provides, and list those earlier steps in depends_on. '
                         'Tag every step with the requirement_ids it implements: the controller derives requirement_coverage from those tags; '
                         'a requirement no step names fails the plan. '
                         'Declare required_test_commands for tests the builder must run natively, using exact capture forms where required. '
                         'Preserve retained_test_commands (owner and previously validated requirements). '
                         'Correct or remove unsupported commands from a rejected proposal when they are not retained requirements. '
                         'native_test_contract lists existing grants; a plan cannot create new ones.')
        with run.phase(PhaseParams(name=f'plan_{version}', kind='agent', owner='planner', retries=1,
                                  description='Draft or correct the complete task plan')) as ph:
            plan_gates = [gates.artifacts_exist, gates.files_non_empty]
            if final:
                plan_gates.append(structure_gate(contract, checks, previous_ids, previous_test_commands))
            try:
                plan = agents.execute(run, ph.phase, AgentCall(output_type=ReviewedPlanOutput,
                    prompt=json.dumps(brief, ensure_ascii=False), previous=prior, gates=plan_gates))
            except agents.GateFailure as error:
                if final and 'plan structure:' in str(error):
                    raise PlanningBlocked(f'Final plan revision is invalid: {error}') from error
                raise
            # The coverage map is derived from the steps; the planner's own map, when it
            # differed, is kept as evidence and never fails the run by itself.
            plan, stated = normalize_plan(plan, contract)
            if stated is not None:
                _record(run, f'coverage-derived-{version}.json',
                        {'planner_stated': stated, 'derived_from_steps': plan.requirement_coverage})
                ph.log(coverage_derived=True)
        previous_documentation = last_plan.documentation if last_plan is not None else None
        last_plan = plan
        _record(run, f'plan-{version}.json', plan.model_dump())
        if on_handoff:
            on_handoff(plan, f'plan_{version}')
        content = _content_digest(plan)
        if content in seen:
            raise PlanningBlocked('Plan made no progress or oscillated to an earlier version')
        seen.add(content)
        responses = {r.finding_id: r for r in plan.finding_responses}
        # A missing or extra response is bookkeeping, not a reason to redo the
        # plan: the reviewer is told which open findings got no response.
        unanswered = sorted(set(unresolved) - set(responses))
        try:
            # The final gate already checked these exact normalized bytes. Test doubles
            # and a changed handoff still take the full deterministic validation path.
            if digest(plan) not in getattr(plan_gates[-1], 'validated', ()):
                validate_plan(plan, contract, checks, run.cfg.defaults.required_test_commands,
                              require_documentation=documentation.required(run), run=run)
            documentation.validate_documentation_plan(plan, contract, previous=previous_documentation)
            resolved_context = selected_context(run, plan, context_packet)
            current_ids = {s.id for s in plan.steps}
            if any(r.disposition == 'needs_owner' for r in responses.values()):
                raise PlanningBlocked('Planner requires an owner decision')
        except (PlanningBlocked, scope.ScopeViolation, ValueError) as error:
            if 'owner decision' in str(error):
                raise
            if final:
                raise PlanningBlocked(f'Final plan revision is invalid: {error}') from error
            finding = PlanFinding(id='CONTROLLER-VALIDATION', severity='blocking',
                                  evidence=str(error), correction='Correct the deterministic plan validation failure')
            unresolved = {**unresolved, finding.id: finding}
            prior = PlanReviewOutput(status='success', summary=str(error), plan_digest=digest(plan),
                                     version=version, decision='revise', findings=list(unresolved.values()))
            _record(run, f'validation-{version}.json', prior.model_dump())
            continue
        if light:
            validate_light_paths(run.cfg, policy, plan.allowed_paths)
            if any((run.repo_root / p).is_dir() for p in plan.allowed_paths):
                raise PlanningBlocked('REQUIRES_FULL_MODE: concrete files required')
        previous_ids = current_ids
        previous_test_commands = set(plan.required_test_commands)
        ordering = order_warnings(plan, getattr(run, 'repo_root', None))
        _record(run, f'order-{version}.json', ordering)
        install_context(run, resolved_context)
        review_brief = _review_brief(brief, resolved_context, plan,
            previous_findings=[f.model_dump() for f in unresolved.values()], unanswered_finding_ids=unanswered,
            final_round=final, controller_order_checks=ordering,
            instructions='Review independently. Bind exactly the supplied digest/version. Close prior findings only after checking corrections; retain unresolved findings.'
                         + (' controller_order_checks lists steps that name something a LATER step '
                            'creates (a file new to the tree, or the type or module named after it). '
                            'For each, confirm the step really depends on it and raise a finding '
                            'naming the step to reorder or split; dismiss it only with a stated reason.'
                            if ordering else '')
                         + (' This is the FINAL review round: there is no further plan revision. '
                            'Every finding you keep becomes a binding note the builder must apply '
                            'and the code reviewer must verify, so state each one as an exact, '
                            'checkable correction.' if final else ''))
        if light:
            review_brief['instructions'] += (' For light mode, explicitly return light_eligible and light_eligibility_reason. Approve eligibility only for a small isolated change with narrow concrete file scope; authentication, authorization, secrets, payments, destructive migrations, public API changes or cross-module architecture require full mode.')
        with run.phase(PhaseParams(name=f'plan_review_{version}', kind='agent', owner='plan_reviewer', retries=1,
                                  description='Independently review this exact plan version')) as ph:
            review = agents.execute(run, ph.phase, AgentCall(output_type=PlanReviewOutput,
                prompt=json.dumps(review_brief, ensure_ascii=False),
                gates=[gates.artifacts_exist, gates.files_non_empty]))
        _record(run, f'review-{version}.json', review.model_dump())
        if on_handoff:
            on_handoff(review, f'plan_review_{version}')
        _assert_review_binding(review, version, plan, 'Plan review failed or returned a stale digest/version')
        found = {f.id: f for f in review.findings}
        resolved = set(review.resolved_finding_ids)
        if (len(found) != len(review.findings) or len(resolved) != len(review.resolved_finding_ids)
                or not resolved <= set(unresolved) or resolved & set(found)):
            raise PlanningBlocked('Invalid or contradictory review finding resolutions')
        for finding in found.values():
            if (finding.id in unresolved
                    and unresolved[finding.id].severity in ('blocking', 'major')
                    and finding.severity == 'minor'):
                raise PlanningBlocked('Review cannot downgrade an unresolved substantive finding')
            if ((finding.requirement_id and finding.requirement_id not in requirements(contract))
                    or (finding.step_id and finding.step_id not in current_ids)):
                raise PlanningBlocked('Review finding references an unknown requirement or step')
        unresolved = {**{k: v for k, v in unresolved.items() if k not in resolved}, **found}
        if review.decision == 'needs_owner':
            raise PlanningBlocked('Plan reviewer requires an owner decision')
        if final and review.decision == 'revise':
            # The final round's findings are binding build notes, not a third round.
            if not unresolved:
                raise PlanningBlocked('Final review asked for revision without findings')
        if final and context_packet.get('conditional_documents') and (review.decision != 'approve'
                or any(f.severity in ('blocking', 'major') for f in unresolved.values())):
            raise PlanningBlocked('Conditional selection requires explicit independent approval without substantive findings')
        if review.decision == 'approve' or final:
            if light and (review.light_eligible is not True or not review.light_eligibility_reason.strip()):
                raise PlanningBlocked('REQUIRES_FULL_MODE: reviewer did not establish light eligibility')
            execution_budget.remaining_timeout(run, 86400)
            if review.decision == 'approve' and not final \
                    and any(f.severity in ('blocking', 'major') for f in unresolved.values()):
                raise PlanningBlocked('Plan approval has unresolved blocking or major findings')
            # Only what the final reviewer actually returned binds the build;
            # an earlier finding it neither closed nor repeated is not re-imposed.
            notes = [f.model_dump() for f in found.values()] if final else []
            if (quality.source_fingerprint(run) != baseline or digest(contract) != frozen_scope
                    or digest(context_packet) != frozen_context
                    or _roles(run) != frozen_roles):
                raise PlanningBlocked('Planning inputs changed during review')
            approval = _approval(run, plan, review,
                request_digest=digest(request), scope_digest=digest(contract), context_digest=frozen_context,
                roles_digest=frozen_roles, baseline_digest=baseline, final_notes=notes, source_digest=_workable(run))
            _record_selection(run, resolved_context)
            _record(run, 'approval.json', approval.model_dump())
            scope.add_plan_requirements(run, [*plan.acceptance_criteria, *final_note_requirements(notes)])
            with run.phase(PhaseParams(name='plan_approved', kind='code', owner='controller',
                                      description='Bind independent plan approval to frozen inputs')) as ph:
                ph.log(version=version, plan_digest=approval.plan_digest,
                       max_plan_repairs=max_repairs, receipt=str(approval_path(run)))
            return plan, approval
        if not unresolved:
            raise PlanningBlocked('Revision requested without actionable findings')
        prior = review.model_copy(update={'findings': list(unresolved.values())})
    raise PlanningBlocked('Plan repair budget exhausted; implementation was not started')


GENERIC_STEMS = {'index', 'main', 'utils', 'util', 'types', 'config', 'models', 'model', 'helpers', 'helper',
                 'tests', 'test', 'init', '__init__', 'readme', 'package', 'module', 'common', 'shared',
                 'constants', 'routes', 'schema', 'settings', 'setup', 'app', 'server', 'client'}


def order_warnings(plan, root) -> list[dict]:
    """Steps that name something a later step creates: a deterministic hint, not a verdict.

    A concrete path absent from the tree is created by the first step that
    lists it. An earlier step whose text names that path, or its file stem as
    a whole word (Swift, Java and Kotlin name files after their type, so
    `FakeExtractor` in a step points at `FakeExtractor.swift`), uses it before
    it exists. Generic stems (index, main, utils ...) are skipped.
    """
    if root is None:
        return []
    root = Path(root)
    created = {}
    for position, step in enumerate(plan.steps):
        for path in step.paths:
            if any(c in path for c in '*?[]') or path.endswith('/') or path in created:
                continue
            try:
                if (root / path).exists():
                    continue
            except (OSError, ValueError):
                continue
            created[path] = position
    warnings = []
    for path, creator in created.items():
        stem = Path(path).name.split('.')[0]
        names = [path]
        if len(stem) >= 5 and stem.lower() not in GENERIC_STEMS:
            names.append(stem)
        for step in plan.steps[:creator]:
            text = ' '.join([step.action, *step.validation, *step.paths])
            for name in names:
                if re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', text):
                    warnings.append({'step_id': step.id, 'uses': name, 'path': path,
                                     'created_by': plan.steps[creator].id})
                    break
    return warnings


def _workable(run) -> str:
    """Builder-writable source fingerprint; '' when the run has no tree to read (bare test doubles)."""
    if getattr(run, 'repo_root', None) is None:
        return ''
    return quality.workable_fingerprint(run)


def _mode(run) -> str:
    policy = getattr(run, "execution_policy", None)
    return policy.mode if policy is not None else 'full'


REUSE_SCAN_LIMIT = 50


def reuse_approved_plan(run, request, contract, context_packet, *, baseline, roles, max_repairs,
                        on_handoff=None):
    """Re-issue an earlier run's approval when nothing planning depends on has changed.

    A task re-run after a failure past planning (a provider outage, a failed
    check, a rejected build) would otherwise pay for planning and plan review
    again. The earlier approval is reused only when every input it was bound
    to is identical now: request, scope, project context (with the accepted
    intent), planner and plan-reviewer configuration, source fingerprint, task
    and execution mode; and only when its plan and review evidence still match
    the digests it recorded and the plan still passes validation. Any
    difference, missing file or mismatch plans from scratch.
    """
    settings = getattr(getattr(run, 'cfg', None), 'execution', None)
    if settings is not None and not getattr(settings, 'reuse_approved_plan', True):
        return None
    sessions = Path(run.session_dir).parent
    if not sessions.is_dir():
        return None
    expected = {'task_id': getattr(run, 'task_id', None), 'request_digest': digest(request),
                'scope_digest': digest(contract), 'context_digest': digest(context_packet),
                'roles_digest': roles, 'mode': _mode(run)}
    workable = None
    candidates = []
    for path in sessions.glob('*/planning/approval.json'):
        if path.parent.parent == Path(run.session_dir) or path.is_symlink():
            continue
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True)[:REUSE_SCAN_LIMIT]:
        try:
            prior = PlanApproval.model_validate(scope._read_json(path))
            if any(getattr(prior, key) != value for key, value in expected.items()):
                continue
            # The code the task works on must be unchanged; control and config
            # files may differ (a role timeout raised between runs, say).
            if prior.source_digest:
                workable = workable or _workable(run)
                if prior.source_digest != workable:
                    continue
            elif prior.baseline_digest != baseline:
                continue
            plan = ReviewedPlanOutput.model_validate(scope._read_json(path.parent / f'plan-{prior.version}.json'))
            review_data = scope._read_json(path.parent / f'review-{prior.version}.json')
            review = PlanReviewOutput.model_validate(review_data)
            if digest(plan) != prior.plan_digest or digest(review_data) != prior.review_digest \
                    or review.plan_digest != prior.plan_digest:
                continue
            validate_plan(plan, contract, run.cfg.defaults.required_quality_checks,
                          run.cfg.defaults.required_test_commands, require_documentation=documentation.required(run), run=run)
            policy = getattr(run, 'execution_policy', None)
            if policy is not None and policy.mode == 'light':
                validate_light_paths(run.cfg, policy, plan.allowed_paths)
            resolved_context = selected_context(run, plan, context_packet)
            if context_packet.get('conditional_documents') and (review.decision != 'approve'
                    or any(f.severity in ('blocking', 'major') for f in review.findings)):
                continue
            lineage = _revision_lineage(prior, path.parent)
            for version, records in lineage:
                for kind, value in records.items():
                    _record_immutable(run, f'{kind}-{version}.json', value)
            plan = _carry_plan_evidence(run, plan, sessions / prior.run_id, prior.run_id)
            review_evidence = _carry_plan_evidence(run, review, sessions / prior.run_id, prior.run_id)
        except Exception:
            continue
        install_context(run, resolved_context)
        _record_selection(run, resolved_context)
        approval = prior.model_copy(update={'run_id': run.adw_id, 'reused_from': prior.run_id,
                                            'reused_plan_digest': prior.plan_digest,
                                            'plan_digest': digest(plan), 'baseline_digest': baseline,
                                            'source_digest': workable})
        _record(run, f'plan-{prior.version}.json', plan.model_dump())
        _record(run, f'review-{prior.version}.json', review_data)
        _record(run, 'approval.json', approval.model_dump())
        scope.add_plan_requirements(run, [*plan.acceptance_criteria, *final_note_requirements(approval.final_notes)])
        if on_handoff:
            on_handoff(plan, 'plan_reused')
            on_handoff(review_evidence, 'plan_review_reused')
        with run.phase(PhaseParams(name='plan_approved', kind='code', owner='controller',
                                  description='Re-issue an unchanged earlier plan approval for this run')) as ph:
            ph.log(version=approval.version, plan_digest=approval.plan_digest, reused_from=prior.run_id,
                   max_plan_repairs=max_repairs, receipt=str(approval_path(run)))
        run.console.note(f'plan: reused the approved plan of run {prior.run_id}; nothing it depends on changed')
        return plan, approval
    return None


def _carry_plan_evidence(run, plan, prior_session, prior_run):
    """Copy an earlier planning envelope's evidence into this run and point it at the copies.

    Only `artifacts` changes (it is not plan content: the content digest
    ignores it); the earlier plan was checked against its recorded digest first.
    """
    target = getattr(run, 'context_handoff_dir', None)
    if target is None:
        return plan
    import shutil
    marker = f'/sessions/{prior_run}/'
    carried = []
    for artifact in plan.artifacts:
        text = Path(artifact).as_posix()
        if marker not in text:
            raise PlanningBlocked('Earlier plan evidence is outside its run')
        tail = Path(text.split(marker, 1)[1])
        if tail.is_absolute() or '..' in tail.parts:
            raise PlanningBlocked('Earlier plan evidence path is unsafe')
        source = prior_session / tail
        if source.is_symlink() or not source.is_file():
            raise PlanningBlocked('Earlier plan evidence is missing')
        destination = Path(target) / f'reused_plan_{prior_run}' / tail
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        carried.append(str(destination))
    return plan.model_copy(update={'artifacts': carried})


def final_note_requirements(notes) -> list[str]:
    return [f"Final plan review note {n['id']} ({n['severity']}): {n['correction']}" for n in notes]


def build_brief(request: str, approval) -> str:
    """The task the builder receives: the request plus the final review's binding notes."""
    if not approval.final_notes:
        return request
    lines = '\n'.join(f"- {n['id']} ({n['severity']}): {n['correction']} [evidence: {n['evidence']}]"
                      for n in approval.final_notes)
    return (f"{request}\n\nFinal plan review notes. The plan was approved with these corrections; apply "
            f"every one while implementing it. The code reviewer will verify each:\n{lines}")


def assert_plan_approval(run, plan, approval, contract, context_packet):
    """Check once before building; expected later candidate edits do not revoke it."""
    validate_plan(plan, contract, run.cfg.defaults.required_quality_checks,
                  run.cfg.defaults.required_test_commands, require_documentation=documentation.required(run), run=run)
    cd.validate_selection(context_packet, plan.context_selection)
    expected = {'run_id': run.adw_id, 'task_id': getattr(run, 'task_id', None),
        'plan_digest': digest(plan), 'request_digest': digest(contract.original_request),
        'scope_digest': digest(contract), 'context_digest': digest(context_packet),
        'roles_digest': _roles(run), 'baseline_digest': quality.source_fingerprint(run)}
    if any(getattr(approval, key) != value for key, value in expected.items()):
        raise PlanningBlocked('Missing, stale or mismatched plan approval')
    saved = scope._read_json(Path(run.session_dir) / 'planning' / 'approval.json')
    if saved != approval.model_dump():
        raise PlanningBlocked('Controller plan approval receipt differs')
    reviewed = scope._read_json(Path(run.session_dir) / 'planning' / f'review-{approval.version}.json')
    if digest(reviewed) != approval.review_digest:
        raise PlanningBlocked('Controller review evidence differs')
