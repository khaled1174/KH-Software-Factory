"""Controller-owned evidence delivered to every later role (SYS-02, SYS-03).

Workers run in isolated copies that deliberately exclude the controller's
session directory, so a reviewer could not read the original records of the
build it judged, and a builder transcribed its own "verbatim" plan. This module
closes that gap without weakening isolation: after each phase the controller
exports selected records into the task's handoff tree, under

    <handoff>/controller_inputs/
        receipts/<time>-<hash>.json  every delivered file: sha256, bytes, producer (newest is current)
        commands/<phase_id>/         command ledger, streams, coverage (SYS-01)
        plan/                        approved plan, approval, review, provenance (SYS-03)
        policy.json                  effective evidence policy and digest (SYS-09)
        findings.json                finding registry (SYS-08)
        resume/                      resumed-work packet (SYS-06)

Every later role's workspace receives a copy. Before any model is invoked the
controller verifies that copy byte-for-byte against the hashes it holds in
memory; a missing or altered required file is an EVIDENCE_DELIVERY_ERROR before
dispatch, not an expensive review that discovers its absence. The originals stay
in the controller session; a worker that edits a delivered copy changes nothing
authoritative and fails file-scope enforcement.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

INPUTS = 'controller_inputs'


class EvidenceDeliveryError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def handoff_root(view) -> Path:
    root = getattr(view, 'handoff_root', None)
    if root is not None:
        return Path(root)
    return Path(view.context_handoff_dir)


def inputs_dir(view) -> Path:
    return handoff_root(view) / INPUTS


def _relative(view, path: Path) -> str:
    return Path(path).resolve().relative_to(handoff_root(view).resolve()).as_posix()


def _expected(view) -> dict:
    expected = getattr(view, 'expected_inputs', None)
    if expected is None:
        expected = {}
        try:
            view.expected_inputs = expected
        except AttributeError:
            pass
    return expected


def _write_receipt(view) -> None:
    directory = inputs_dir(view)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {'schema_version': 1, 'run_id': getattr(view, 'adw_id', ''),
               'written_at': datetime.now(timezone.utc).isoformat(),
               'files': dict(sorted(_expected(view).items())),
               'phases': dict(getattr(view, 'delivered_phases', {}) or {}),
               # What this packet is evidence for: plan identity and, once checked,
               # the exact candidate bytes (FIX-01).
               'identity': dict(getattr(view, 'packet_identity', {}) or {}),
               'note': 'Copies of controller records. Originals stay in the controller session; '
                       'the controller verifies these bytes before every model dispatch.'}
    # Evidence files are write-once (publication refuses to replace one), so each
    # delivery writes a new, complete receipt; the newest one is current.
    import time
    receipts = directory / 'receipts'
    receipts.mkdir(parents=True, exist_ok=True)
    data = json.dumps(receipt, indent=2, ensure_ascii=False).encode('utf-8')
    name = f'{time.time_ns():020d}-{hashlib.sha256(data).hexdigest()[:12]}.json'
    (receipts / name).write_bytes(data)


def deliver_file(view, source: Path | None, relative: str, *, producer: str, kind: str, data: bytes | None = None,
                 provenance: str = ''):
    """Place one controller record under controller_inputs and register its hash.

    Delivering the same identity with the same bytes again is a no-op; different
    bytes under an existing identity are a conflict, never an overwrite (FIX-01).
    """
    target = inputs_dir(view) / relative
    if target.is_symlink() or any(part == '..' for part in Path(relative).parts):
        raise EvidenceDeliveryError(f'EVIDENCE_DELIVERY_ERROR: unsafe delivery path {relative}')
    if data is None:
        if source is None or Path(source).is_symlink() or not Path(source).is_file():
            raise EvidenceDeliveryError(f'EVIDENCE_DELIVERY_ERROR: source record missing or a symlink: {source}')
        data = Path(source).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if target.exists():
        if _sha(target) != digest:
            raise EvidenceDeliveryError(f'EVIDENCE_DELIVERY_CONFLICT: {relative} already holds different bytes; '
                                        'a delivered record is never overwritten')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name('.' + target.name + '.partial')
        temporary.write_bytes(data)
        temporary.replace(target)                 # atomic publication
    key = _relative(view, target)
    entry = {'sha256': digest, 'bytes': len(data), 'producer': producer, 'kind': kind}
    if source is not None:
        entry['source'] = provenance or str(source)
    _expected(view)[key] = entry
    return target


def deliver_json(view, relative: str, value, *, producer: str, kind: str):
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True).encode('utf-8')
    target = deliver_file(view, None, relative, producer=producer, kind=kind, data=data)
    _write_receipt(view)
    return target


def _phases(view) -> dict:
    phases = getattr(view, 'delivered_phases', None)
    if phases is None:
        phases = {}
        view.delivered_phases = phases
    return phases


def refresh_receipt(view) -> None:
    _write_receipt(view)


def _native(view, phase_id: str, native_dir) -> list[str]:
    """The runtime's own event stream of every send of the phase, copied unchanged."""
    delivered = []
    if native_dir is None or not Path(native_dir).is_dir():
        return delivered
    for raw in sorted(Path(native_dir).glob('attempt-*/raw_output.jsonl')):
        try:
            # The controller runs in the owner project root: provenance is project-relative.
            provenance = raw.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            provenance = str(raw)
        deliver_file(view, raw, f'commands/{phase_id}/native/{raw.parent.name}.raw_output.jsonl',
                     producer=phase_id, kind='native_runtime_stream', provenance=provenance)
        delivered.append(raw.parent.name)
    return delivered


def export_commands(view, phase_id: str, agent: str, ledger, decisions=None, *, status='completed',
                    native_dir=None) -> dict:
    """Copy one finished phase's command records into the handoff (SYS-02, FIX-01)."""
    from . import command_ledger
    phases = _phases(view)
    native = _native(view, phase_id, native_dir)
    if ledger is None:
        phases[phase_id] = {'agent': agent, 'delivery': 'unavailable', 'phase_status': status,
                            'native_streams': native,
                            'reason': 'this runtime emitted no tool events the controller could record'}
        _write_receipt(view)
        return phases[phase_id]
    ledger.finalize()
    records = ledger.invocations()
    base = f'commands/{phase_id}'
    deliver_file(view, ledger.path, f'{base}/{command_ledger.LEDGER_NAME}', producer=phase_id, kind='command_ledger')
    unavailable = []
    for record in records:
        stream = record.get('stream') or {}
        if not stream.get('path'):
            unavailable.append(record['invocation_id'])
            continue
        source = ledger.directory / stream['path']
        if not source.is_file() or _sha(source) != stream.get('sha256'):
            raise EvidenceDeliveryError(f'EVIDENCE_DELIVERY_ERROR: controller stream for {record["invocation_id"]} '
                                        'differs from its recorded digest')
        deliver_file(view, source, f'{base}/{stream["path"]}', producer=phase_id, kind='raw_stream')
    routed = ledger.directory / 'exec'
    if routed.is_dir():
        for path in sorted(routed.rglob('*')):
            if path.is_file() and not path.is_symlink():
                deliver_file(view, path, f'{base}/exec/{path.relative_to(routed).as_posix()}', producer=phase_id,
                             kind='kh_exec_record')
    rows = command_ledger.coverage(records, decisions)
    deliver_file(view, None, f'{base}/coverage.json', producer='controller', kind='command_coverage',
                 data=json.dumps(rows, indent=2, ensure_ascii=False).encode('utf-8'))
    phases[phase_id] = {'agent': agent, 'delivery': 'exported', 'phase_status': status,
                        'native_streams': native,
                        'invocations': len(records),
                        'gaps': sum(1 for r in rows if r['resolution'] == 'gap'),
                        'streams_unavailable': unavailable,
                        'unavailable_reason': 'interrupted before a result was received' if unavailable else '',
                        'recording_errors': list(getattr(ledger, 'errors', []) or [])}
    _write_receipt(view)
    return phases[phase_id]


def verify(view, receipt_path=None) -> None:
    """Before dispatch: the recipient's copy of every delivered record is intact.

    With `receipt_path`, the verified inventory is recorded as this dispatch's
    delivery receipt (in the controller session, outside every workspace).
    """
    expected = getattr(view, 'expected_inputs', None)
    if not expected:
        return
    root = handoff_root(view)
    problems = []
    for relative, meta in expected.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            problems.append(f'{relative} is missing')
        elif _sha(path) != meta['sha256']:
            problems.append(f'{relative} differs from the controller record')
    if problems:
        raise EvidenceDeliveryError('EVIDENCE_DELIVERY_ERROR: controller inputs were not delivered intact to '
                                    'this role (' + '; '.join(problems[:20]) + '); no model was invoked')
    if receipt_path is not None:
        receipt_path = Path(receipt_path)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps({'verified_at': datetime.now(timezone.utc).isoformat(),
                                            'workspace_handoff': str(root),
                                            'identity': dict(getattr(view, 'packet_identity', {}) or {}),
                                            'files': dict(sorted(expected.items()))}, indent=2), encoding='utf-8')


def find_invocation(view, invocation_id: str):
    """A command record by invocation ID: this phase's ledger, then delivered ledgers."""
    ledger = getattr(view, 'command_ledger', None)
    if ledger is not None and invocation_id in ledger.records:
        return ledger.records[invocation_id]
    directory = inputs_dir(view) / 'commands'
    if directory.is_dir():
        for path in sorted(directory.rglob('ledger.jsonl')):
            merged = {}
            for line in path.read_text(encoding='utf-8').splitlines():
                event = json.loads(line)
                if event.get('invocation_id') == invocation_id:
                    merged.update(event)
            if merged:
                return merged
    return None


def _when(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def redirects_to(record: dict, path: Path) -> bool:
    """The recorded command redirected output to a file of this name, written during the call."""
    command = str(record.get('command') or '')
    if '>' not in command or Path(path).name not in command:
        return False
    started, ended = _when(record.get('started_at')), _when(record.get('ended_at'))
    if started is None or ended is None:
        return False
    modified = Path(path).stat().st_mtime
    return started - 1 <= modified <= ended + 2


# ── plan provenance (SYS-03) ────────────────────────────────────────────────

ALGORITHM = ('sha256 over json.dumps(model_dump(mode="json"), sort_keys=True, separators=(",", ":"), '
             'ensure_ascii=False)')


def deliver_plan(view, plan, approval, session_dir: Path) -> dict:
    """The complete approved plan and its approval chain, as controller records."""
    from . import planning
    planning_dir = Path(session_dir) / 'planning'
    prefix = f'plan/revisions/{approval.version}' if approval.parent_approval_digest else 'plan'
    names = {'plan.json': planning_dir / f'plan-{approval.version}.json',
             'review.json': planning_dir / f'review-{approval.version}.json',
             'approval.json': planning_dir / 'approval.json'}
    for version, records in planning._revision_lineage(approval, planning_dir):
        for kind in records:
            names[f'lineage/{version}/{kind}.json'] = planning_dir / f'{kind}-{version}.json'
    hashes = {}
    for name, source in names.items():
        if not source.is_file():
            raise EvidenceDeliveryError(f'EVIDENCE_DELIVERY_ERROR: controller plan record {source.name} is missing')
        target = deliver_file(view, source, f'{prefix}/{name}', producer='planning', kind='plan_record')
        hashes[name] = _sha(target)
    recomputed = planning.digest(plan)
    coverage = {req: sorted(step.id for step in plan.steps if req in step.requirement_ids)
                for req in plan.requirement_coverage}
    reuse = {}
    if approval.reused_from:
        earlier = Path(session_dir).parent / approval.reused_from / 'planning' / f'plan-{approval.version}.json'
        unchanged = None
        if earlier.is_file():
            try:
                earlier_plan = type(plan).model_validate(json.loads(earlier.read_text(encoding='utf-8')))
                unchanged = planning._content_digest(earlier_plan) == planning._content_digest(plan)
            except Exception:
                unchanged = None
        reuse = {'reused_from': approval.reused_from, 'reused_plan_digest': approval.reused_plan_digest,
                 'content_unchanged': unchanged,
                 'explanation': 'This run re-issued an earlier approval. plan_digest differs from '
                                'reused_plan_digest only because carried evidence paths (artifacts) moved; '
                                'content_digest excludes those fields and is the identity of the plan content.'}
    provenance = {
        'schema_version': 1, 'run_id': approval.run_id, 'task_id': approval.task_id, 'version': approval.version,
        'digest_algorithm': ALGORITHM,
        'plan_digest': approval.plan_digest, 'plan_digest_verified': recomputed == approval.plan_digest,
        'content_digest': planning._content_digest(plan),
        'content_digest_excludes': ['artifacts', 'finding_responses', 'observations', 'notes_for_next_agent',
                                    'commit_message', 'summary'],
        'file_sha256': hashes,
        'request_digest': approval.request_digest,
        'request_digest_note': 'identifies the request text only; it is never plan provenance',
        'review_digest': approval.review_digest, 'final_notes': approval.final_notes,
        'allowed_paths': list(plan.allowed_paths), 'steps': len(plan.steps),
        'requirements': sorted(plan.requirement_coverage),
        'requirement_coverage': plan.requirement_coverage,
        'coverage_inversion': coverage,
        'coverage_verified': all(sorted(v) == coverage[k] for k, v in plan.requirement_coverage.items()),
        'reuse': reuse,
        'revision': {'parent_approval_digest': approval.parent_approval_digest,
                     'parent_approval_version': approval.parent_approval_version,
                     'reason': approval.revision_reason, 'required_paths': approval.revision_paths,
                     'candidate_digest': approval.revision_candidate_digest},
        'rules': ['Reference these files by path; do not transcribe the plan.',
                  'Any shortened view you write must say it is a projection and list what it omits.',
                  'A request digest alone is not plan provenance.'],
    }
    deliver_json(view, f'{prefix}/provenance.json', provenance, producer='controller', kind='plan_provenance')
    view.active_plan_record_prefix = prefix
    return provenance


def plan_instruction(view) -> str:
    directory = inputs_dir(view) / getattr(view, 'active_plan_record_prefix', 'plan')
    if not (directory / 'provenance.json').is_file():
        return ''
    return (f"\nAuthoritative plan records (controller-delivered, read-only): {directory}/plan.json (complete "
            f"structured plan), approval.json, review.json, provenance.json (digests, algorithm, coverage map, "
            f"reuse explanation), and lineage/ (immutable parent records when this is a revision). "
            f"Reference these by path. Do not write your own 'verbatim' copy of the plan; any "
            f"shortened view must be labelled a projection and list what it omits. A request digest is not plan "
            f"provenance.")


def commands_instruction(view) -> str:
    directory = inputs_dir(view)
    text = ''
    if (directory / 'owner_supplied').is_dir():
        text = (f"\nOwner-supplied original records (unchanged copies; source path and sha256 in the newest "
                f"receipt): {directory}/owner_supplied/. Judge them as originals of what they are; they restore "
                f"what they contain and nothing more.")
    if not (directory / 'commands').is_dir():
        return text
    return (f"\nController command records of earlier phases (read-only): {directory}/commands/<phase>/ — "
            f"ledger.jsonl (exact command, timestamps, runtime outcome, analysed status), streams/ (the complete "
            f"output each command returned) and coverage.json (how each strict command's evidence requirement is "
            f"met). The newest file in receipts/ lists every delivered file with its sha256. These are the authoritative command "
            f"evidence: judge execution history from them, and do not ask for records that are not listed; an "
            f"entry marked unknown or gap is genuinely unknown. Each phase also has native/ (the runtime's own "
            f"event stream, unchanged) and, for commands run through adws/kh_exec.py, exec/ (complete streams and "
            f"verified receipts).") + text


# ── inventory and owner decision drafts (FIX-03) ─────────────────────────────

def _coverage_rows(view) -> list[dict]:
    rows = []
    for path in sorted((inputs_dir(view)).rglob('coverage.json')):
        try:
            for row in json.loads(path.read_text(encoding='utf-8')):
                rows.append({**row, 'coverage_file': _relative(view, path)})
        except (OSError, ValueError):
            continue
    return rows


def inventory(view) -> dict:
    """Every delivered record, by kind and producer, and every strict command's evidence state."""
    by_kind: dict[str, list] = {}
    for relative, meta in sorted((getattr(view, 'expected_inputs', None) or {}).items()):
        by_kind.setdefault(meta['kind'], []).append({'path': relative, 'sha256': meta['sha256'],
                                                     'bytes': meta['bytes'], 'producer': meta['producer'],
                                                     **({'source': meta['source']} if meta.get('source') else {})})
    rows = _coverage_rows(view)
    return {'schema_version': 1, 'identity': dict(getattr(view, 'packet_identity', {}) or {}),
            'records_by_kind': by_kind,
            'strict_commands': [r for r in rows if r.get('strict')],
            'gaps': [r for r in rows if r.get('resolution') == 'gap'],
            'note': 'Everything the controller holds for this candidate is listed here. A record that is not '
                    'listed was never captured; a gap stays unknown and is never reconstructed.'}


def decision_draft(view, reviewed, categories, task) -> dict:
    """A concrete, not-in-force draft for the owner: what is missing and what a decision would cover."""
    import re as _re
    prefix = _re.sub(r'[^A-Z0-9]', '', str(task or 'TASK').upper()) or 'TASK'
    if not prefix[0].isalpha():
        prefix = 'T' + prefix
    # The run is part of the draft ID, so drafts of different runs never collide.
    run = _re.sub(r'[^A-Z0-9]', '', str(getattr(view, 'adw_id', '') or '').upper())[:8] or 'RUN'
    prefix = f'{prefix}-R{run}'
    gaps = [r for r in _coverage_rows(view) if r.get('resolution') == 'gap']
    drafts = []
    for index, gap in enumerate(gaps, 1):
        drafts.append({'id': f'{prefix}-X{index}', 'task': str(task), 'kind': 'historical_exception',
                       'authority': '', 'recorded_at': '', 'invocations': [gap['invocation_id']],
                       'classes': [gap.get('class', 'other')],
                       'substitution': '', 'does_not_establish': '',
                       'context': {'command': gap.get('command', ''), 'why': gap.get('why', ''),
                                   'status': gap.get('status')}})
    return {'schema_version': 1, 'in_force': False,
            'instruction': 'Draft only. Nothing here is a decision. To decide, copy the entries you accept into '
                           '.kh/dispositions.json, fill authority, recorded_at (now), substitution and '
                           'does_not_establish, and remove the context field. Findings that name records outside '
                           'KH (for example an older harness session) need their own invocation IDs.',
            'identity': dict(getattr(view, 'packet_identity', {}) or {}),
            'findings': [{'text': text, 'category': category}
                         for text, category in zip(reviewed.blocking, categories)],
            'uncovered_invocations': gaps,
            'draft_decisions': drafts}
