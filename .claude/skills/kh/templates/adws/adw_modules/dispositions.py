"""Owner evidence decisions as records the controller applies mechanically (SYS-09).

An owner decision is an owner-authored record in `.kh/dispositions.json`.
No worker workspace contains it. Example:

    {"schema_version": 1,
     "decisions": [{
        "id": "T3-X1",
        "task": "T1",
        "kind": "historical_exception",
        "authority": "Owner (name), where and when the decision was taken",
        "recorded_at": "2000-01-01T15:00:00Z",
        "invocations": ["example-run#1"],
        "classes": ["format"],
        "substitution": "what evidence is accepted instead",
        "does_not_establish": "what this decision does not prove"}]}

Rules enforced here, not by a prompt:

- A historical exception matches exact invocation IDs only (no patterns), of
  the listed classes only, and only invocations that started before the
  decision was recorded: it can never become a waiver for future commands.
- Classification comes from the controller's command record; a test or
  formatting command cannot become exploratory compilation by relabelling.
- Prospective rules are the command classes in `execution.command_policy`
  (strict or excerpt-only); they apply to every run identically.
- The effective policy (classes + applicable decisions) has one digest. It is
  delivered to every worker as a controller input and written into the receipt.
- A model's output that cites an owner decision without a matching record, or
  applies a decision to an invocation it does not name, fails its gate.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

FILE = Path('.kh') / 'dispositions.json'
ID = re.compile(r'^[A-Z][A-Z0-9]*(?:[-.][A-Z0-9]+)+$')
KINDS = {'historical_exception'}
# Keyword case-insensitive, ID case-sensitive and shaped like a decision ID
# (T3-X1): ordinary prose such as "exception (non-fatal)" or "decision: re-run" is not a citation.
_CITATION = re.compile(r'\b(?i:owner[\s-]+)?(?i:exception|disposition|decision|waiver)\s*[:#]?\s*'
                       r'\(?([A-Z][A-Z0-9]*\d[A-Z0-9]*(?:-[A-Z0-9]+)+|[A-Z][A-Z0-9]*-[A-Z]*\d[A-Z0-9]*(?:-[A-Z0-9]+)*)\)?(?![\w-]|\.\w)')
_INVOCATION = re.compile(r'\b[\w.-]*#\d+\b')


class DispositionError(ValueError):
    pass


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError as error:
        raise DispositionError(f'recorded_at is not an ISO-8601 time: {value!r}') from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load(root, task_id=None, class_names=()) -> list[dict]:
    """Validated decisions that apply to this task. A malformed file stops the run."""
    path = Path(root) / FILE
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise DispositionError(f'{FILE} must be a regular file')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as error:
        raise DispositionError(f'{FILE} is not valid JSON: {error}') from error
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('decisions'), list):
        raise DispositionError(f'{FILE} needs {{"schema_version": 1, "decisions": [...]}}')
    seen, result = set(), []
    known = set(class_names)
    for raw in data['decisions']:
        if not isinstance(raw, dict):
            raise DispositionError('each decision must be an object')
        allowed = {'id', 'task', 'kind', 'authority', 'recorded_at', 'invocations', 'classes',
                   'substitution', 'does_not_establish'}
        unknown = set(raw) - allowed
        if unknown:
            raise DispositionError(f'decision {raw.get("id")!r}: unknown fields {sorted(unknown)}')
        ident = raw.get('id')
        if not isinstance(ident, str) or not ID.match(ident):
            raise DispositionError(f'decision id must look like T3-X1: {ident!r}')
        if ident in seen:
            raise DispositionError(f'duplicate decision id {ident}')
        seen.add(ident)
        if raw.get('kind') not in KINDS:
            raise DispositionError(f'{ident}: kind must be one of {sorted(KINDS)}')
        for field in ('authority', 'substitution', 'does_not_establish'):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise DispositionError(f'{ident}: {field} is required')
        _time(raw.get('recorded_at'))
        invocations = raw.get('invocations')
        if (not isinstance(invocations, list) or not invocations
                or any(not isinstance(i, str) or not i.strip() or any(c in i for c in '*?[]') for i in invocations)):
            raise DispositionError(f'{ident}: invocations must list exact invocation IDs (no patterns)')
        classes = raw.get('classes', [])
        if not isinstance(classes, list) or not classes or any(not isinstance(c, str) for c in classes):
            raise DispositionError(f'{ident}: classes must name the command classes it covers')
        if known and set(classes) - known - {'other'}:
            raise DispositionError(f'{ident}: unknown command classes {sorted(set(classes) - known)}')
        if not isinstance(raw.get('task'), str) or not raw['task'].strip():
            raise DispositionError(f'{ident}: task is required (decisions bind to one task)')
        if task_id is not None and raw['task'] != task_id:
            continue
        result.append(dict(raw))
    return result


def match(decisions, record: dict):
    """The decision covering this exact invocation, or None."""
    for decision in decisions or []:
        if record.get('invocation_id') not in decision['invocations']:
            continue
        if record.get('class_class', 'other') not in decision['classes']:
            continue
        started = record.get('started_at')
        if started and _time(started) > _time(decision['recorded_at']):
            continue                 # a decision never waives a later execution
        return decision
    return None


def policy_record(cfg, decisions) -> dict:
    """The single effective evidence policy for this run, with its digest."""
    classes = [c.model_dump() for c in cfg.execution.command_policy.classes]
    body = {'schema_version': 1, 'command_classes': classes,
            'require_complete_strict_records': cfg.execution.command_policy.require_complete_strict_records,
            'decisions': decisions,
            'rules': [
                'Classification is by the controller-recorded command text; agent labels are ignored.',
                'Strict classes need an authentic exit status and complete output in the controller record.',
                'Non-strict classes need exact command and timing records; a filtered display is an excerpt.',
                'An excerpt never counts as passing evidence; the controller quality checks decide checks.',
                'An unrecorded original exit status is unknown; a later re-run is a new execution.',
                'A historical exception applies only to the exact invocation IDs and classes it lists, '
                'and only to invocations that started before it was recorded.']}
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return {**body, 'digest': hashlib.sha256(text.encode()).hexdigest()}


def instruction(record: dict, path: str) -> str:
    return (f"\nController evidence policy (digest {record['digest']}; full record: {path}): command evidence "
            "is the controller's own record of every shell command; strict classes "
            f"({', '.join(c['name'] for c in record['command_classes'] if c['strict'])}) need an authentic exit "
            "status and complete output; other classes need command and timing only. Owner decisions in force: "
            + (', '.join(f"{d['id']} (invocations {', '.join(d['invocations'])}; classes {', '.join(d['classes'])})"
                         for d in record['decisions']) or 'none')
            + ". Cite an owner decision only by an ID listed here and only for the invocations it names.")


def claims_gate(envelope, run):
    """Model output cannot claim an owner exception that has no matching record."""
    from .data_types import GateReport
    report = GateReport()
    decisions = {d['id']: d for d in (getattr(run, 'owner_decisions', None) or [])}
    registry = getattr(run, 'findings_registry', None)
    known_findings = set(getattr(registry, 'findings', {}) or {})
    text_parts = [envelope.summary, envelope.notes_for_next_agent]
    for field in ('blocking',):
        text_parts.extend(str(x) for x in getattr(envelope, field, []) or [])
    for finding in getattr(envelope, 'findings', []) or []:
        values = finding.model_dump() if hasattr(finding, 'model_dump') else finding
        if isinstance(values, dict):
            text_parts.extend(str(v) for v in values.values() if isinstance(v, str))
    problems = []
    for part in text_parts:
        for sentence in re.split(r'(?<=[.;\n])\s+', str(part or '')):
            for cited in _CITATION.findall(sentence):
                if cited.startswith(('R', 'S', 'F')) and re.fullmatch(r'[RSF]\d+(?:[-.]\d+)*', cited):
                    continue        # requirement / step / finding IDs, not decisions
                if cited in known_findings:
                    continue        # a review finding ID, not an owner decision
                decision = decisions.get(cited)
                if decision is None:
                    problems.append(f'cites owner decision {cited}, which has no record in {FILE}')
                    continue
                named = set(_INVOCATION.findall(sentence))
                outside = sorted(named - set(decision['invocations']))
                if outside:
                    problems.append(f'applies decision {cited} to {", ".join(outside)}, which it does not name')
    report.check('owner decision citations', not problems,
                 'every cited decision has a matching record' if not problems else
                 '; '.join(dict.fromkeys(problems)) + '. Remove the claim or cite a recorded decision exactly.')
    return report
