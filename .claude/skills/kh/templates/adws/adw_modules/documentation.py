"""Documentation proposals under the existing controller, scope and review gates."""
from __future__ import annotations
from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from . import scope, conditional_docs as cd


def documentation_path(path):
    parsed = PurePosixPath(path)
    if (not path or parsed.is_absolute() or parsed.as_posix() != path or '..' in parsed.parts
            or any(c in path for c in '*?[]\\') or any(ord(c) < 32 for c in path)
            or not path.endswith('.md') or not path.startswith(('app_docs/', 'docs/agent-guide/'))
            or parsed.name in {'CLAUDE.md', 'AGENTS.md', 'SKILL.md'}):
        raise ValueError('Invalid documentation path: ' + path)
    return path


class DocumentationPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decision: Literal['required', 'not_needed']
    reason: str = Field(min_length=1, max_length=2000)
    paths: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def coherent(self):
        if not self.reason.strip():
            raise ValueError('Documentation impact requires a concrete reason')
        if (self.decision == 'required') != bool(self.paths):
            raise ValueError('Required documentation needs paths; not_needed must have none')
        if len(set(self.paths)) != len(self.paths):
            raise ValueError('Documentation paths must be unique')
        for path in self.paths:
            documentation_path(path)
        return self


def required(run):
    return run.cfg.execution.documentation.enabled


def validate_documentation_plan(plan, contract, *, required=False, previous=None):
    impact = getattr(plan, 'documentation', None)
    if impact is None:
        if required or previous is not None and previous.decision == 'required':
            raise ValueError('Plan must declare documentation impact and preserve required documentation')
        return
    # Revalidate even model_copy updates used by internal callers.
    impact = DocumentationPlan.model_validate(impact.model_dump())
    if previous is not None and previous.decision == 'required':
        if impact.decision != 'required' or not set(previous.paths) <= set(impact.paths):
            raise ValueError('Plan revisions must preserve required documentation paths')
    for path in impact.paths:
        try:
            scope.plan_boundary(contract, [path])
            scope.plan_boundary(contract.model_copy(update={'allowed_paths': plan.allowed_paths}), [path])
        except (ValueError, scope.ScopeViolation) as error:
            raise ValueError('Plan documentation path is outside the approved scope: ' + path) from error


def planning_contract(run):
    guides = []
    if required(run) and pc._read(run.repo_root, pc.INDEX) is not None:
        guides = affected_guides(run, json.loads(pc._read(run.repo_root, pc.INDEX)),
                                 run.scope_contract.allowed_paths)
    return {'enabled': required(run), 'decision_required': required(run), 'affected_guides': guides,
            'instructions': 'When enabled, declare documentation as {decision: required|not_needed, reason: concrete reason, paths: exact Markdown paths}. '
                            'Use app_docs for historical change records and docs/agent-guide for current guides. '
                            'New or changed read_when conditions require docs/agent-guide/conditional_docs.md as an exact planned target. '
                            'The controller renders that catalog from reviewed conditions before checks; do not expand scope implicitly. '
                            'Include every target inside allowed_paths and owner scope. Preserve earlier required documentation. '
                            'Affected current guides within owner scope must be updated or confirmed; out-of-scope guides are deferred as stale. '
                            'Root instructions, skills, settings and permissions are not documenter targets. '
                            'With this feature disabled, legacy plans may omit documentation (null). A not_needed decision needs a behavioral reason; it cannot waive a requested deliverable.'}


# This output is a proposal, never a worker source-write permission.
from pathlib import Path
import json
from . import agents, gates, permissions, project_context as pc, quality, workspace
from .data_types import AgentCall, EnvelopeBase, PhaseParams


class GuideConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class DocumentationProposalOutput(EnvelopeBase):
    model_config = ConfigDict(extra='forbid')
    candidate_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    documents: list[pc.ContextDocument] = Field(min_length=1, max_length=32)
    confirmed_documents: list[GuideConfirmation] = Field(default_factory=list, max_length=128)

    @model_validator(mode='before')
    @classmethod
    def discard_legacy_coverage(cls, value):
        # Coverage is controller-owned; old replies may still include the key.
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key != 'documented_paths'}
        return value


def affected_guides(run, prior, paths, *, contract=None):
    paths = ['**'] if paths is None else paths
    observed = pc._observed(Path(run.repo_root), prior.get('excluded_paths', []),
                            prior.get('generated_paths'), prior.get('dependency_paths'))
    result = []
    for row in prior['documents']:
        if contract is not None and contract.allowed_paths is not None and not any(
                scope._matches(row['path'], pattern) for pattern in contract.allowed_paths):
            continue
        if not any(pc._overlap(pattern, path) for pattern in pc._applicability(row['path'], row.get('applies_to'))
                   for path in paths):
            continue
        sources = pc.source_digests(run.repo_root, row, observed)
        if sources is not None:
            result.append({**{key: value for key, value in row.items() if key != 'source_digests'},
                           'content': pc._read(run.repo_root, row['path']),
                           'source_digest': pc.digest(sources)})
    return result


def _validate_confirmations(run, prior, documents, confirmations, changed_paths):
    proposed = {document.path for document in documents}
    expected = {row['path']: row for row in affected_guides(run, prior, changed_paths, contract=getattr(getattr(run, '_owner', run), 'scope_contract', None))
                if row['path'] not in proposed}
    paths = [row.path for row in confirmations]
    if len(set(paths)) != len(paths) or set(paths) != set(expected):
        raise ValueError('Confirm every affected guide or update it in approved paths: ' +
                         ', '.join(sorted(set(paths) ^ set(expected))))
    for confirmation in confirmations:
        row = expected[confirmation.path]
        if (confirmation.digest != row['digest'] or pc.digest(row['content']) != row['digest']
                or confirmation.source_digest != row['source_digest']):
            raise ValueError('Guide confirmation differs from current document/source: ' + confirmation.path)


def _source_proofs(run, rows, prior):
    observed = pc._observed(Path(run.repo_root), prior.get('excluded_paths', []),
                            prior.get('generated_paths'), prior.get('dependency_paths'))
    result = {}
    for row in rows:
        sources = pc.source_digests(run.repo_root, row, observed)
        if sources is not None:
            result[row['path']] = {'digest': row['digest'],
                'applies_to': pc._applicability(row['path'], row.get('applies_to')), 'source_digests': sources}
    return result


def validate_source_proofs(run, receipt):
    """Never replace review-time source proof with a new candidate's hashes."""
    if receipt.get('decision') != 'required':
        return
    prior = json.loads(pc._read(run.repo_root, pc.INDEX))
    indexed = {row['path']: row for row in prior['documents']}
    confirmations = [GuideConfirmation.model_validate(row) for row in receipt.get('confirmed_documents', [])]
    proposed = [pc.ContextDocument(path=row['path'], content=pc._read(run.repo_root, row['path']),
        **{key: row[key] for key in ('applies_to', *pc.DocumentLifecycle.model_fields) if key in row})
        for row in receipt['documents']]
    _validate_confirmations(run, prior, proposed, confirmations, receipt.get('documented_paths', []))
    rows = [*receipt['documents'], *[indexed[row.path] for row in confirmations]]
    for row in rows:
        if pc.digest(pc._read(run.repo_root, row['path'])) != row['digest']:
            raise ValueError('Documentation source proof document changed: ' + row['path'])
    if receipt.get('source_proofs', {}) != _source_proofs(run, rows, prior):
        raise ValueError('Documentation source proof changed or missing')


def validate_setup(run, plan):
    if plan.documentation and plan.documentation.decision == 'required' and not required(run):
        raise ValueError('Required documentation stage is not configured; enable execution.documentation explicitly')
    if required(run) and plan.documentation and plan.documentation.decision == 'required':
        agent = agents.resolve(run.cfg, 'documenter')
        agent.writes = []
        agents.validate(run.cfg, ['documenter'])
        for path in plan.documentation.paths:
            if not permissions.permitted(path, agents.resolve(run.cfg, 'builder'), run.cfg):
                raise ValueError('Documentation target is protected or outside builder writes: ' + path)


def _original_content(run, path):
    baseline = getattr(run, 'task_baseline', getattr(run, 'agent_baseline', None))
    entry = baseline.entries.get(path) if baseline is not None else None
    if baseline is None:
        raise ValueError('Documentation requires an owner source baseline')
    if entry is None:
        return None
    if entry.kind != 'file':
        raise ValueError('Documentation owner target is not a regular file: ' + path)
    return entry.data.decode('utf-8')


def _validate_documents(run, plan, documents, prior):
    paths = [document.path for document in documents]
    if len(set(paths)) != len(paths) or set(paths) != set(plan.documentation.paths):
        raise ValueError('Documentation must cover exactly the approved documentation paths'
            + '; extra=' + json.dumps(sorted(set(paths) - set(plan.documentation.paths)))
            + '; missing=' + json.dumps(sorted(set(plan.documentation.paths) - set(paths)))
            + '; duplicate=' + json.dumps(sorted(path for path in set(paths) if paths.count(path) > 1)))
    old_rows = {row['path']: row for row in prior['documents']}
    for document in documents:
        pc.resolve_document_metadata(document, old_rows.get(document.path))
    rows = dict(old_rows)
    rows.update({document.path: document.model_dump() for document in documents
                 if document.path.startswith('docs/agent-guide/')})
    pc.validate_lifecycles(list(rows.values()))
    cd.prepare_catalog(documents, list(rows.values()), lambda path: _original_content(run, path),
                       previous=list(old_rows.values()))
    for document in documents:
        path = documentation_path(document.path)
        scope.check_paths(run.scope_contract, [path])
        if not permissions.permitted(path, agents.resolve(run.cfg, 'builder'), run.cfg):
            raise ValueError('Documentation target is protected: ' + path)
        pc._path(run.repo_root, path)
        if not document.applies_to:
            raise ValueError('Documentation requires explicit applicability: ' + path)
        if not document.content.strip() or len(document.content.encode('utf-8')) > pc.MAX_BYTES:
            raise ValueError('Documentation must be nonempty and bounded: ' + path)
        pc.validate_library_claims(run.repo_root, document)
        pc.validate_command_guide(run.repo_root, document, pc.command_declarations(run.repo_root, run.cfg, {'manifests': {}}))
        for target in pc._links(path, document.content):
            if target not in paths and not pc._path(run.repo_root, target).exists():
                raise ValueError('Broken documentation link: ' + target)
        original = _original_content(run, path)
        if path.startswith('app_docs/'):
            if original is not None:
                raise ValueError('Preserve historical write-ups; choose a new documentation path: ' + path)
            if document.purpose != 'change_record' or document.status != 'historical':
                raise ValueError('app_docs records must be historical change records')
        elif original is not None:
            row = old_rows.get(path, {})
            if not row.get('managed') or row.get('digest') != pc.digest(original):
                raise ValueError('Preserve owner documentation verbatim: ' + path)


def _baseline_receipt(run, plan):
    if not required(run):
        return {'schema_version': 1, 'status': 'not_configured', 'decision': 'unassessed',
                'reason': 'Legacy configuration: execution.documentation.enabled is false', 'paths': [],
                'candidate_sha256': quality.source_fingerprint(run), 'documents': [], 'artifacts': []}
    validate_documentation_plan(plan, run.scope_contract, required=True)
    return {'schema_version': 1, 'status': 'candidate', 'decision': plan.documentation.decision,
            'reason': plan.documentation.reason, 'paths': list(plan.documentation.paths), 'documents': [], 'artifacts': []}


def prepare_candidate(run, plan, changed_paths, *, attempt=1, on_handoff=None):
    """Controller writes scoped docs before checks; models only propose content."""
    receipt = _baseline_receipt(run, plan)
    if receipt['decision'] != 'required':
        receipt['candidate_sha256'] = quality.source_fingerprint(run)
        return receipt
    validate_setup(run, plan)
    changed_paths = sorted(set(changed_paths))
    before = quality.source_fingerprint(run)
    prior_raw = pc._read(run.repo_root, pc.INDEX)
    prior = json.loads(prior_raw)
    def proposal_gate(output, view):
        from .data_types import GateReport
        report = GateReport()
        try:
            if output.candidate_sha256 != before:
                raise ValueError('Documentation proposal must bind the measured candidate')
            _validate_documents(run, plan, output.documents, prior)
            _validate_confirmations(run, prior, output.documents, output.confirmed_documents, changed_paths)
            report.check('documentation proposal', True)
        except (ValueError, scope.ScopeViolation, OSError) as error:
            report.check('documentation proposal', False, str(error))
        return report
    with run.phase(PhaseParams(name=f'document_{attempt}', kind='agent', owner='documenter', retries=1,
                               description='Propose scoped change records and current guides without source writes')) as ph:
        proposal = agents.execute(run, ph.phase, AgentCall(output_type=DocumentationProposalOutput,
            prompt=json.dumps({'candidate_sha256': before, 'changed_paths': changed_paths,
                'documentation': plan.documentation.model_dump(),
                'indexed_documents': [row for row in prior['documents'] if row['path'] in plan.documentation.paths],
                'affected_guides': affected_guides(run, prior, changed_paths, contract=getattr(run, '_owner', run).scope_contract),
                'instruction': 'READ-ONLY PROPOSAL. Inspect the current source and actual change. Return inline documents for exactly documentation.paths, no other targets. '
                               'The controller records documented_paths from its measured changed_paths; do not emit that field. '
                               'app_docs files are historical change_record records; docs/agent-guide files are current guides unless explicitly superseded. '
                               'For existing indexed paths, omitted metadata (including read_when) inherits indexed_documents; any transition must be explicit. '
                               'For each affected_guides entry not updated in approved paths, inspect current source and return confirmed_documents '
                               '[{path, digest, source_digest}] with the supplied identities only if the unchanged guide is still accurate. '
                               'Confirmations grant no write permission or metadata change. Only affected_guides requires confirmation; the controller defers out-of-scope guides as stale for the next reviewed context refresh. '
                               'For current feature guides propose read_when [{id: stable-lowercase-id, condition: natural-language task condition}]. '
                               'Never make mandatory instructions conditional. Historical app_docs records are not current guides. '
                               'When conditions change, the approved paths must include docs/agent-guide/conditional_docs.md; return that target with empty content. '
                               'The controller renders its exact content before candidate checks and review. '
                               'Do not write repository documentation or code. Only current phase evidence may be written. '
                               'Describe implemented behavior; configured checks and independent review follow this stage.'}, ensure_ascii=False),
            gates=[gates.artifacts_exist, gates.files_non_empty, proposal_gate]))
    if on_handoff is not None:
        on_handoff(proposal, f'document_{attempt}')
    workspace.require_role_cleanup(run)
    if quality.source_fingerprint(run) != before:
        raise ValueError('Documentation worker changed the candidate')
    # Validate the complete batch before any write. Stage failure discards all docs.
    _validate_documents(run, plan, proposal.documents, prior)
    _validate_confirmations(run, prior, proposal.documents, proposal.confirmed_documents, changed_paths)
    for document in proposal.documents:
        pc._atomic(run.repo_root, document.path, document.content)
    receipt.update(input_candidate_sha256=before, candidate_sha256=quality.source_fingerprint(run),
        documented_paths=changed_paths, prior_index_digest=pc.digest(prior_raw),
        documents=[{'path': doc.path, 'digest': pc.digest(doc.content), 'applies_to': doc.applies_to,
                    **pc.lifecycle(doc.path, doc.model_dump())} for doc in proposal.documents],
        confirmed_documents=[row.model_dump() for row in proposal.confirmed_documents],
        artifacts=[Path(path).relative_to(run.repo_root).as_posix() if Path(path).is_absolute() else path
                   for path in proposal.artifacts])
    indexed = {row['path']: row for row in prior['documents']}
    receipt['source_proofs'] = _source_proofs(run,
        [*receipt['documents'], *[indexed[row.path] for row in proposal.confirmed_documents]], prior)
    return receipt


def restore_candidate(run, plan, receipt):
    """A review-only continuation uses saved checked docs; never dispatch or edit."""
    if not isinstance(receipt, dict) or receipt.get('schema_version') != 1:
        raise ValueError('REVIEW_EXISTING_DOCUMENTATION_MISSING: run the task normally')
    if receipt.get('candidate_sha256') != quality.source_fingerprint(run):
        raise ValueError('Saved documentation candidate changed')
    if required(run):
        validate_documentation_plan(plan, run.scope_contract, required=True)
        if receipt.get('decision') != plan.documentation.decision or receipt.get('paths') != plan.documentation.paths:
            raise ValueError('Saved documentation differs from approved plan')
    if receipt.get('decision') == 'required':
        rows = receipt.get('documents', [])
        if {row['path'] for row in rows} != set(receipt['paths']) or len(rows) != len(receipt['paths']):
            raise ValueError('Saved documentation coverage differs')
        for row in rows:
            documentation_path(row['path'])
            scope.check_paths(run.scope_contract, [row['path']])
            if pc.digest(pc._read(run.repo_root, row['path'])) != row['digest']:
                raise ValueError('Saved documentation changed: ' + row['path'])
            pc.lifecycle(row['path'], row)
        if receipt.get('prior_index_digest') != pc.digest(pc._read(run.repo_root, pc.INDEX)):
            raise ValueError('Saved documentation context baseline changed')
        validate_source_proofs(run, receipt)
        combined = {row['path']: row for row in json.loads(pc._read(run.repo_root, pc.INDEX))['documents']}
        combined.update({row['path']: row for row in rows if row['path'].startswith('docs/agent-guide/')})
        pc.validate_lifecycles(list(combined.values()))
        cd.validate_catalog(list(combined.values()), lambda path: pc._read(run.repo_root, path))
    return receipt


def context_projection(run, receipt):
    """Prepare index metadata for the exact reviewed candidate; no filesystem writes."""
    if receipt['decision'] != 'required':
        return None
    if receipt['candidate_sha256'] != quality.source_fingerprint(run):
        raise ValueError('Documentation candidate changed before context projection')
    validate_source_proofs(run, receipt)
    prior_raw = pc._read(run.repo_root, pc.INDEX)
    if receipt['prior_index_digest'] != pc.digest(prior_raw):
        raise ValueError('Context index changed during documentation')
    index = json.loads(prior_raw)
    rows = {row['path']: row for row in index['documents']}
    for row in receipt['documents']:
        if pc.digest(pc._read(run.repo_root, row['path'])) != row['digest']:
            raise ValueError('Documentation content changed after its proposal')
        if row['path'].startswith('docs/agent-guide/'):
            rows[row['path']] = {**row, 'managed': True, 'provenance': 'controller-generated'}
    for path, proof in receipt.get('source_proofs', {}).items():
        if path in rows:
            rows[path] = {**rows[path], 'source_digests': proof['source_digests']}
    pc.validate_lifecycles(list(rows.values()))
    cd.validate_catalog(list(rows.values()), lambda path: pc._read(run.repo_root, path))
    observed = pc._observed(Path(run.repo_root), index.get('excluded_paths', []),
                            index.get('generated_paths'), index.get('dependency_paths'))
    index['documentation_provenance'] = {'run_id': run.adw_id,
        'checked_candidate_sha256': receipt['candidate_sha256'],
        'document_manifest_sha256': pc.digest(receipt['documents'])}
    index.update(documents=[rows[path] for path in sorted(rows)],
                 structure_digest=pc.digest(observed['files']), manifest_digests=observed['manifests'])
    index.pop('digest', None);index['digest'] = pc.digest(index)
    pc._context_packet(run.repo_root, {'status': 'ready', 'index': index,
        'digest': index['digest'], 'observed': observed}, '', [])
    serialized = json.dumps(index, indent=2) + '\n'
    if len(serialized.encode()) > pc.MAX_BYTES:
        raise ValueError('Documentation context index exceeds bounded size')
    return {'prior_index_digest': receipt['prior_index_digest'], 'observed_digest': pc.digest(observed),
            'index': index, 'index_text': serialized}


def finish(run, receipt, projection):
    """After source promotion, publish only approved index metadata before completion."""
    from .scope import _atomic_json
    target = Path(run.session_dir)/'documentation.json'
    result = {**receipt, 'run_id': run.adw_id, 'checks_passed': True, 'review_approved': True}
    try:
        workspace.require_role_cleanup(run)
        if projection is not None:
            index = projection['index']
            if pc.digest(pc._read(run.repo_root, pc.INDEX)) != projection['prior_index_digest']:
                raise ValueError('Owner context index changed before publication')
            observed = pc._observed(Path(run.repo_root), index.get('excluded_paths', []),
                                    index.get('generated_paths'), index.get('dependency_paths'))
            if pc.digest(observed) != projection['observed_digest']:
                raise ValueError('Promoted source differs from reviewed documentation candidate')
            validate_source_proofs(run, receipt)
            pc._atomic(run.repo_root, pc.INDEX, projection['index_text'])
            result['context_digest'] = index['digest']
            result['stale_source_conflicts'] = [c for c in pc.inspect_context(run.repo_root)['conflicts']
                                                if c.startswith('Guide source changed: ')]
        if receipt['status'] != 'not_configured':
            result['status'] = 'published' if projection else 'not_needed'
        _atomic_json(target, result)
    except BaseException as error:
        try:
            _atomic_json(target, {**result, 'status': 'publication_failed', 'error': str(error),
                                 'recovery': 'Source promoted; reconcile completion and use reviewed context refresh.'})
        except BaseException as recording_error:
            error.add_note('Could not record documentation publication failure: ' + str(recording_error))
        raise
    return result
