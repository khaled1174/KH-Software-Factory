"""Keep a task's unaccepted work between runs, so a re-run continues instead of restarting.

A guarded task builds in a staging copy and promotes nothing until checks and
review pass. Before this module, a run that stopped (role timeout, provider
failure, failed checks, rejected review) threw that copy away, and the next run
of the same task started from an empty tree.

Now the controller keeps what the stopped run produced, inside the project:

    .kh/work/<task>/current/
        meta.json       which run, why it stopped, the owner tree and plan it belongs to,
                        the last real review (kept until a newer one exists), the last
                        failure (separate from the review), refused paths with their
                        owner-baseline identity, finding registry, checked-candidate record
        manifest.json   every changed path: kind, mode, sha256 (null = deleted)
        files/...       the bytes of those paths
        progress.json   the builder's own step report, when it wrote one (a claim)
        plan.md         the approved plan with those steps marked, for the owner
        evidence/       the stopped run's controller records (command ledgers, ...)
        checks/         check logs of a checked candidate, for a review-only continuation

The next run of the same task applies it to its fresh staging copy, after the
plan is approved, and tells the builder to continue. It is applied only when the
owner tree is byte-identical to the one the work was built on and the plan's
content is the same; otherwise it is moved to stale/<run> and the run starts
clean. Applied work is a candidate like any other: checks, scope review and the
independent review all run on it before anything reaches the owner tree.
Nothing here is visible to agents (the whole of .kh/work is excluded from
every copy), and the folder is cleared when the task is accepted.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path, PurePosixPath

from . import permissions

WORK_DIR = Path('.kh') / 'work'
SCHEMA = 2
READABLE_SCHEMAS = (1, 2)    # both saved-state schemas are readable


def task_key(task_id, contract_digest: str) -> str:
    key = task_id or ('adhoc-' + contract_digest[:12])
    if not key or '/' in key or key.startswith('.') or any(c in key for c in '\\:\0'):
        raise ValueError(f'Unsafe task key for resumable work: {key!r}')
    return key


def work_dir(root, key) -> Path:
    return Path(root) / WORK_DIR / key


def _safe(rel: str, excluded) -> bool:
    path = PurePosixPath(rel)
    if (not rel or path.is_absolute() or '..' in path.parts or rel.startswith('@git/')
            or path.parts[0] == '.git' or path.as_posix() != rel):
        return False
    return not any(rel == x.rstrip('/') or rel.startswith(x.rstrip('/') + '/') for x in excluded)


def _plan_markdown(plan, progress) -> str:
    done = set((progress or {}).get('done') or [])
    lines = [f'# Task plan (approved)', '', plan.summary, '',
             'Steps marked [x] are the ones the builder reported done; that is its claim, '
             'checked again by the controller before acceptance.', '']
    for step in plan.steps:
        lines.append(f"- [{'x' if step.id in done else ' '}] {step.id}: {step.action}")
        lines.append(f"  - files: {', '.join(step.paths)}")
    if (progress or {}).get('next'):
        lines += ['', f"Builder's last note: {progress['next']}"]
    return '\n'.join(lines) + '\n'


def _latest_progress(handoff: Path):
    found = sorted(handoff.rglob('builder/progress.json'), key=lambda p: (p.stat().st_mtime, str(p)))
    for path in reversed(found):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            done = [str(s) for s in data.get('done', []) if isinstance(s, (str, int))][:200]
            return {'done': done, 'next': str(data.get('next', ''))[:2000]}
    return None


def _read_progress(path: Path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def baseline_identity(baseline, rel):
    """What the owner tree held at a path before the task: its sha256, or absent."""
    entry = getattr(baseline, 'entries', {}).get(rel) if baseline is not None else None
    if entry is None:
        return {'state': 'absent from the owner baseline'}
    return {'state': 'present in the owner baseline', 'kind': entry.kind,
            'sha256': hashlib.sha256(entry.data).hexdigest()}


def failure_kind(reason: str) -> str:
    text = str(reason or '')
    for marker, kind in (('EXECUTION_DEADLINE', 'deadline'), ('DEADLINE_INSUFFICIENT', 'deadline'),
                         ('CANDIDATE_CHECKED_UNREVIEWED', 'deadline'), ('BUDGET_EXHAUSTED', 'budget'),
                         ('ROLE TIMEOUT', 'role_timeout'), ('Changes exceed task scope', 'scope'),
                         ('modified prohibited paths', 'permission'), ('INFRA_BOUNDARY', 'infrastructure'),
                         ('EVIDENCE_DELIVERY', 'evidence_delivery'), ('provider outage', 'provider'),
                         ('not accepted within the configured repair budget', 'review_rejected'),
                         ('REVIEW_EXISTING_REJECTED', 'review_rejected'), ('failed gates', 'gates')):
        if marker in text:
            return kind
    return 'other'


def save(root, key, *, run_id, reason, owner_digest, plan, plan_content, view, changed, excluded, feedback='',
         last_review=None, rejected=None, checked=None, findings=None, evidence_dir=None, last_build=None):
    """Store the stopped run's candidate changes next to the project source, never in it.

    The last real review survives a later failure that produced none (a scope or
    transport stop), and the failure is recorded apart from it (SYS-06).
    """
    target = work_dir(root, key)
    staging = target / f'.saving-{_slug(run_id)}'
    current = permissions.snapshot(view)
    manifest = {}
    previous_meta = _read_progress(target / 'current' / 'meta.json') or {}
    if staging.exists():
        shutil.rmtree(staging)
    (staging / 'files').mkdir(parents=True)
    for rel in changed:
        if not _safe(rel, excluded):
            continue
        entry = current.entries.get(rel)
        if entry is None:
            manifest[rel] = None
            continue
        record = {'kind': entry.kind, 'mode': entry.mode, 'sha256': hashlib.sha256(entry.data).hexdigest()}
        if entry.kind == 'file':
            destination = staging / 'files' / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(entry.data)
        elif entry.kind == 'symlink':
            record['target'] = entry.data.decode('utf-8', errors='surrogateescape')
        manifest[rel] = record
    previous = target / 'current'
    # A run that stopped before its builder wrote a note keeps the last one.
    progress = _latest_progress(Path(view.context_handoff_dir)) or _read_progress(previous / 'progress.json')
    review = last_review or previous_meta.get('last_review')
    meta = {'schema': SCHEMA, 'task': key, 'run_id': run_id, 'saved_at': time.time(),
            'reason': str(reason or ''),
            # Free-text compatibility field; never silently emptied by a later stop.
            'feedback': str(feedback or '') or str(previous_meta.get('feedback') or ''),
            'last_failure': {'run_id': run_id, 'kind': failure_kind(reason), 'message': str(reason or '')},
            'last_review': review,
            'rejected': list(rejected or []),
            'applied': {rel: (record or {}).get('sha256') for rel, record in manifest.items()},
            'checked': checked,
            'findings': findings if findings is not None else previous_meta.get('findings'),
            'last_build': last_build,
            'owner_digest': owner_digest, 'plan_content': plan_content, 'paths': sorted(manifest)}
    if evidence_dir is not None and Path(evidence_dir).is_dir():
        shutil.copytree(evidence_dir, staging / 'evidence' / Path(evidence_dir).name, symlinks=False)
    elif (target / 'current' / 'evidence').is_dir():
        shutil.copytree(target / 'current' / 'evidence', staging / 'evidence', symlinks=False)
    if checked and checked.get('logs'):
        for name, source in checked['logs'].items():
            if Path(source).is_file():
                (staging / 'checks').mkdir(exist_ok=True)
                shutil.copyfile(source, staging / 'checks' / (name + '.log'))
        meta['checked'] = {k: v for k, v in checked.items() if k != 'logs'}
        meta['checked']['logs'] = sorted(checked['logs'])
    (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8')
    (staging / 'meta.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    if progress:
        (staging / 'progress.json').write_text(json.dumps(progress, indent=2), encoding='utf-8')
    if plan is not None:
        (staging / 'plan.md').write_text(_plan_markdown(plan, progress), encoding='utf-8')
    # Never a moment without a complete copy: move the old one aside, then swap.
    retired = target / f'.retired-{_slug(run_id)}'
    if retired.exists():
        shutil.rmtree(retired)
    if previous.exists():
        os.replace(previous, retired)
    os.replace(staging, previous)
    shutil.rmtree(retired, ignore_errors=True)
    for leftover in target.glob('.saving-*'):
        shutil.rmtree(leftover, ignore_errors=True)
    return meta


def _slug(value) -> str:
    text = ''.join(c if c.isalnum() or c in '-_' else '_' for c in str(value))[:64]
    return text or 'unknown'


def _archive(target: Path, why: str):
    current = target / 'current'
    meta = _read_progress(current / 'meta.json') or {}
    stale = target / 'stale' / f"{_slug(meta.get('run_id', 'unknown'))}-{time.time_ns()}"
    stale.parent.mkdir(parents=True, exist_ok=True)
    os.replace(current, stale)
    (stale / 'why-not-resumed.txt').write_text(why + '\n', encoding='utf-8')
    return stale


def _load(current: Path, owner_digest, plan_content, excluded, allowed):
    """Validated entries to apply, or a reason not to. Raises nothing for bad data."""
    meta = json.loads((current / 'meta.json').read_text(encoding='utf-8'))
    manifest = json.loads((current / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(meta, dict) or not isinstance(manifest, dict):
        return meta if isinstance(meta, dict) else {}, None, 'kept work is malformed'
    if meta.get('schema') not in READABLE_SCHEMAS:
        return meta, None, 'saved by another KH version'
    if meta.get('owner_digest') != owner_digest:
        return meta, None, 'the project changed since that work was built on it'
    if meta.get('plan_content') != plan_content:
        return meta, None, 'the approved plan is different'
    entries = {}
    for rel, record in sorted(manifest.items()):
        if not isinstance(rel, str) or not _safe(rel, excluded) or not allowed(rel):
            return meta, None, f'kept work names a path the builder may not write: {rel}'
        if record is None:
            entries[rel] = None
            continue
        if (not isinstance(record, dict) or record.get('kind') not in ('file', 'symlink', 'directory')
                or type(record.get('mode')) is not int or not isinstance(record.get('sha256'), str)):
            return meta, None, f'kept work is malformed at {rel}'
        if record['kind'] == 'file':
            source = current / 'files' / rel
            data = source.read_bytes() if source.is_file() and not source.is_symlink() else None
        elif record['kind'] == 'symlink':
            data = str(record.get('target', '')).encode('utf-8', errors='surrogateescape')
        else:
            data = b''
        if data is None or hashlib.sha256(data).hexdigest() != record['sha256']:
            return meta, None, f'kept file does not match its recorded digest: {rel}'
        entries[rel] = permissions.Entry(record['kind'], record['mode'], data)
    return meta, entries, None


def apply(root, key, *, owner_digest, plan_content, stage_root, excluded, allowed=lambda rel: True, archive=True):
    """Apply kept work to a fresh staging copy. Returns (meta, progress) or (None, note).

    Anything wrong with the kept work moves it to stale/ so the task is never
    stuck on it; a failure while writing into the stage does the same and then
    stops this run, whose stage may be half-written.
    """
    target = work_dir(root, key)
    current = target / 'current'
    if not current.is_dir():
        return None, ''
    try:
        meta, entries, why = _load(current, owner_digest, plan_content, excluded, allowed)
    except Exception as error:
        meta, entries, why = {}, None, f'kept work is unreadable: {error}'
    if why is not None and not archive:
        # A read-only caller (advisory review) never moves the saved candidate.
        return None, f'kept work from run {meta.get("run_id", "unknown")} does not apply ({why}); left unchanged'
    if why is not None:
        stale = _archive(target, why)
        return None, f'kept work from run {meta.get("run_id", "unknown")} was not resumed ({why}); moved to {stale}'
    root_path = Path(stage_root)
    try:
        for rel in sorted((r for r, e in entries.items() if e is None), key=lambda p: p.count('/'), reverse=True):
            path = root_path / rel
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                permissions.restore_entry(path, None)
        for rel in sorted((r for r, e in entries.items() if e is not None), key=lambda p: p.count('/')):
            path = root_path / rel
            if path.is_dir() and not path.is_symlink() and entries[rel].kind != 'directory':
                shutil.rmtree(path)
            permissions.restore_entry(path, entries[rel])
    except Exception as error:
        if not archive:
            raise RuntimeError(f'Kept work could not be applied to the review copy ({error}); left unchanged') from error
        stale = _archive(target, f'could not be applied: {error}')
        raise RuntimeError(f'Kept work could not be applied ({error}); moved to {stale}. '
                           'Run the task again: it starts clean.') from error
    return meta, _read_progress(current / 'progress.json')


BRIEF_REVIEW_CHARS = 20_000


def brief(meta, progress) -> str:
    """What the builder is told when it continues a stopped run's work."""
    paths = meta.get('paths') or []
    shown = ', '.join(paths[:60]) + (f' (+{len(paths) - 60} more)' if len(paths) > 60 else '')
    failure = meta.get('last_failure') or {}
    text = (f"RESUMING THIS TASK: an earlier run ({meta.get('run_id')}) stopped before the task was accepted "
            f"(stop kind: {failure.get('kind', 'unrecorded')}; {meta.get('reason') or 'no reason recorded'}). "
            f"Its applied work is already in your workspace; these paths differ from the project: "
            f"{shown or '(none)'}. The controller packet resume/applied.json lists each applied path's sha256.")
    rejected = meta.get('rejected') or []
    if rejected:
        lines = []
        for item in rejected[:60]:
            base = item.get('baseline') or {}
            lines.append(f"{item['path']} ({base.get('state', 'baseline unknown')}"
                         + (f", sha256 {base['sha256']}" if base.get('sha256') else '') + ')')
        text += (" REFUSED PATHS: the stopped run changed these paths and the controller refused them; their "
                 "edits were NOT kept. Each is exactly as in the owner's baseline now: " + '; '.join(lines) + ". "
                 "A path present in the baseline existed before this task: do not delete, move or edit it "
                 "unless the approved plan's allowed paths include it.")
    review = meta.get('last_review')
    if isinstance(review, dict):
        blocking = review.get('blocking') or []
        listed = '\n'.join(f'- {b}' for b in blocking)
        if len(listed) > BRIEF_REVIEW_CHARS:
            listed = listed[:BRIEF_REVIEW_CHARS] + '\n… (complete list: controller packet resume/last_review.json)'
        text += (f"\nLAST INDEPENDENT REVIEW (run {review.get('run_id')}, {review.get('phase')}, "
                 f"{'approved' if review.get('approved') else 'rejected'}; candidate {review.get('candidate_sha256') or 'unrecorded'}): "
                 f"{review.get('summary', '')}" + (f"\nIts blocking findings, all still to be resolved:\n{listed}" if blocking else '')
                 + "\nA finding is resolved only when fixed in the code; changed bytes alone resolve nothing.")
    done = progress.get('done') if isinstance(progress, dict) else None
    done = [str(d)[:40] for d in done][:200] if isinstance(done, list) else []
    if done:
        text += (" The previous builder reported these plan steps done: " + ', '.join(done)
                 + " (its own claim: verify before relying on it).")
    if isinstance(progress, dict) and progress.get('next'):
        text += f" Its last note: {str(progress['next'])[:2000]}"
    if meta.get('feedback'):
        text += f" What stopped the work last time (fix this first): {meta['feedback']}"
    return text + (" Continue from there: keep and complete the existing work and fix what is broken. "
                   "Do not start over and do not repeat finished steps.")


def write_plan(root, key, plan, progress=None):
    """The task's approved plan, in the project, for the owner to read while the task runs."""
    target = work_dir(root, key)
    target.mkdir(parents=True, exist_ok=True)
    (target / 'plan.md').write_text(_plan_markdown(plan, progress), encoding='utf-8')


def clear(root, key):
    """The task was accepted: its kept work and plan copy are done with (stale/ is kept)."""
    target = work_dir(root, key)
    current = target / 'current'
    if current.exists():
        shutil.rmtree(current, ignore_errors=True)
    (target / 'plan.md').unlink(missing_ok=True)


PROGRESS_INSTRUCTION = ("After you finish each plan step, rewrite {path} as JSON "
                        "{{\"done\": [ids of the finished plan steps], \"next\": \"what you are doing now\"}}. "
                        "If this run stops before acceptance, the next run continues from your work and this note.")


def deliver_packet(view, meta, root, key) -> None:
    """Put the resume record and the stopped run's controller evidence in the new run's handoff."""
    from . import evidence_packet
    current = work_dir(root, key) / 'current'
    evidence_packet.deliver_json(view, 'resume/meta.json',
                                 {k: v for k, v in meta.items() if k not in ('owner_digest',)},
                                 producer='resume', kind='resume_record')
    evidence_packet.deliver_json(view, 'resume/applied.json', meta.get('applied') or
                                 {p: None for p in meta.get('paths') or []}, producer='resume', kind='resume_record')
    if meta.get('last_review'):
        evidence_packet.deliver_json(view, 'resume/last_review.json', meta['last_review'],
                                     producer='resume', kind='resume_record')
    evidence = current / 'evidence'
    if evidence.is_dir():
        for path in sorted(evidence.rglob('*')):
            if path.is_file() and not path.is_symlink() and 'receipts' not in path.parts:
                relative = path.relative_to(evidence).as_posix()
                evidence_packet.deliver_file(view, path, f"resumed/{_slug(meta.get('run_id'))}/{relative}",
                                             producer=f"run {meta.get('run_id')}", kind='resumed_record')
        evidence_packet.deliver_json(view, 'resume/index.json',
                                     {'resumed_from': meta.get('run_id'),
                                      'records': f"resumed/{_slug(meta.get('run_id'))}/"},
                                     producer='resume', kind='resume_record')
