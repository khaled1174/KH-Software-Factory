"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .data_types import EnvelopeBase, GateReport

REPORT_PATHS_SHOWN = 60  # paths named back to a builder whose changed_files claim is wrong
TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def _artifact(path, run):
    root = Path(run.repo_root).resolve() if run is not None else Path.cwd().resolve()
    p = Path(path)
    if not p.is_absolute(): p = root / p
    if not p.resolve().is_relative_to(root):
        raise ValueError("artifact escapes the execution workspace")
    return p


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _resolve(path: str, declaration, run) -> Path:
    """A declared `base: evidence` path resolves in the phase evidence directory;
    every other relative path resolves against the workspace root."""
    if declaration is not None and declaration.base == 'evidence' and not Path(path).is_absolute():
        evidence = getattr(run, 'context_handoff_dir', None)
        if evidence is None:
            raise ValueError('base "evidence" needs a phase evidence directory')
        candidate = Path(evidence) / path
        return _artifact(str(candidate), run)
    return _artifact(path, run)


def _empty_allowed(declaration, path: Path, run) -> tuple[bool, str]:
    """An empty file is legitimate only as a raw stream authenticated by a controller command record."""
    if declaration is None or declaration.kind != 'raw_stream':
        return False, ('empty file: a report or data artifact must have content. An empty raw stream is '
                       'accepted when declared in artifact_declarations with kind "raw_stream" and the '
                       'invocation_id of the controller command record that produced it')
    if not declaration.invocation_id:
        return False, 'an empty raw stream needs the invocation_id of the command that produced it'
    from . import evidence_packet
    record = evidence_packet.find_invocation(run, declaration.invocation_id)
    if record is None:
        return False, (f'no controller command record {declaration.invocation_id}; an empty stream cannot be '
                       'authenticated without one')
    stream = record.get('stream') or {}
    for routed in record.get('kh_exec') or []:
        # An empty stdout/stderr of the controller execution route, authenticated by
        # its verified receipt (the file must be the one that receipt names).
        listed = routed.get(path.name) if path.name in ('stdout', 'stderr') else None
        if (routed.get('verified') and isinstance(listed, dict) and listed.get('bytes') == 0
                and path.parent.name == routed.get('id')):
            return True, f'authenticated empty {path.name} of kh_exec receipt {routed.get("id")}'
    if stream.get('bytes') == 0:
        return True, f'authenticated empty stream: controller record {declaration.invocation_id} captured no output'
    if evidence_packet.redirects_to(record, path):
        return True, (f'authenticated empty stream: {declaration.invocation_id} redirected output to this file '
                      'and the file was written during that invocation')
    return False, (f'controller record {declaration.invocation_id} neither captured an empty stream nor '
                   f'redirected output to {path.name} during its execution')


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    required = type(envelope).__name__ in {"PlanOutput", "ReviewedPlanOutput", "PlanReviewOutput", "ProjectContextProposal", "ContextReviewOutput", "ReviewOutput", "DocumentOutput", "ScoutOutput"}
    declarations = {d.path: d for d in getattr(envelope, 'artifact_declarations', []) or []}
    paths = list(dict.fromkeys([*envelope.artifacts, *declarations]))
    report.check("required artifacts", bool(paths) or not required,
                 "role must provide at least one concrete evidence file")
    root = Path(run.repo_root).resolve() if run is not None else Path.cwd().resolve()
    evidence = getattr(run, 'context_handoff_dir', None) if run is not None else None
    for a in paths:
        declaration = declarations.get(a)
        try:
            p = _resolve(a, declaration, run)
            if not p.is_file():
                note = "artifact must be an existing file inside the workspace"
                if (evidence is not None and not Path(a).is_absolute()
                        and (Path(evidence) / a).is_file()):
                    note = (f"relative artifact paths resolve against the workspace root ({root}), not your "
                            f"evidence directory; {a!r} exists at {Path(evidence) / a}: list that full path, "
                            f"or declare it with base \"evidence\" in artifact_declarations")
                elif not Path(a).is_absolute() and '/' not in a:
                    note += f" (a bare file name resolves to {root / a})"
                report.check(a, False, note)
                continue
            if p.stat().st_size == 0:
                ok, why = _empty_allowed(declaration, p, run)
                report.check(a, ok, why)
            else:
                report.check(a, True, _size(p))
                if declaration is not None and declaration.sha256:
                    report.check(a + ': sha256', _sha256(p) == declaration.sha256,
                                 'declared sha256 must match the file')
                if declaration is not None and declaration.bytes is not None:
                    report.check(a + ': bytes', p.stat().st_size == declaration.bytes,
                                 'declared size must match the file')
            if (getattr(run, 'scope_contract', None) is not None or type(envelope).__name__ in
                    {'ReviewedPlanOutput', 'PlanReviewOutput', 'ProjectContextProposal', 'ContextReviewOutput'}):
                report.check(a + ': phase evidence', p.resolve().is_relative_to(Path(run.context_handoff_dir).resolve()),
                             'scoped roles must supply evidence from their own current phase directory')
        except (ValueError, OSError) as e:
            report.check(a, False, str(e))
    if type(envelope).__name__ in {"PlanOutput", "ReviewedPlanOutput"}:
        criteria = envelope.acceptance_criteria
        report.check("acceptance criteria", bool(criteria) and all(x.strip() for x in criteria),
                     "plan requires explicit nonempty acceptance criteria")
        contract = getattr(run, "scope_contract", None)
        if contract is not None:
            from .scope import ScopeViolation, plan_boundary
            try:
                plan_boundary(contract, list(envelope.allowed_paths))
                report.check("allowed_paths", True, "plan declares a bounded source boundary")
            except (ScopeViolation, ValueError) as e:
                report.check("allowed_paths", False,
                             f"{e}; declare the exact files/patterns this task must change, within owner scope")
    if type(envelope).__name__ == "DocumentOutput":
        report.check("document_path", bool(envelope.document_path) and envelope.document_path in envelope.artifacts,
                     "document_path must identify a declared evidence artifact")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    return artifacts_exist(envelope, run)


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        try:
            p = _artifact(a, run)
        except (ValueError, OSError) as e:
            report.check(a, False, str(e))
            continue
        if p.suffix != ".json": continue
        if not p.is_file():
            report.check(a, False, "JSON artifact is missing")
            continue
        try:
            parsed = json.loads(p.read_text())
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except (json.JSONDecodeError, OSError) as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def diff_matches_claims(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    if run is None or not hasattr(run, "agent_baseline"):
        return report.check("baseline", False, "no controller-captured change baseline")
    from . import permissions
    after = permissions.snapshot(run)
    paths = permissions.changed_paths(run.agent_baseline, after)
    own = getattr(run, "agent_output_relative", "")
    actual = set()
    for path in paths:
        if path.startswith("@git/") or path == own or path.startswith(own + "/"):
            continue
        if permissions.structural_directory(path, run.agent_baseline, after): continue
        actual.add(path)
    claimed = set()
    for path in getattr(envelope, "changed_files", []):
        try:
            p = _artifact(path, run)
            claimed.add(p.relative_to(Path(run.repo_root).resolve()).as_posix())
        except (ValueError, OSError) as e:
            report.check(path, False, str(e))
    note = f"unreported={sorted(actual-claimed)}; unchanged/incorrect={sorted(claimed-actual)}"
    if claimed != actual:
        listed = sorted(actual)
        shown = listed[:REPORT_PATHS_SHOWN]
        more = f" (+{len(listed) - len(shown)} more)" if len(listed) > len(shown) else ""
        note += (f". The controller measured this change set: {shown}{more}. Only your report is wrong: set "
                 "changed_files to exactly the measured set and do not edit any file to fix this")
    report.check("exact changed-file set (this phase)", claimed == actual, note)
    cumulative = list(getattr(envelope, "cumulative_task_changed_files", []) or [])
    task_baseline = getattr(run, "task_baseline", None)
    if cumulative and task_baseline is not None:
        sessions = (Path(run.cfg.defaults.data_dir) / "sessions").as_posix() if getattr(run, "cfg", None) else ""
        task_actual = set()
        for path in permissions.changed_paths(task_baseline, after):
            if (path.startswith("@git/") or path == own or path.startswith(own + "/")
                    or (sessions and (path == sessions or path.startswith(sessions + "/")))):
                continue
            if permissions.structural_directory(path, task_baseline, after): continue
            task_actual.add(path)
        claimed_task = set()
        for path in cumulative:
            try:
                claimed_task.add(_artifact(path, run).relative_to(Path(run.repo_root).resolve()).as_posix())
            except (ValueError, OSError) as e:
                report.check(path, False, str(e))
        report.check("cumulative task change set", claimed_task == task_actual,
                     "cumulative_task_changed_files is the whole task's delta (resumed and earlier attempts "
                     "included), not this phase's: " + (f"controller measured {sorted(task_actual)[:REPORT_PATHS_SHOWN]}"
                     if claimed_task != task_actual else "matches the controller task delta"))
    elif cumulative:
        report.check("cumulative task change set", True, "no controller task baseline in this workflow; not checked")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    findings = list(getattr(envelope, "findings", []))
    report.check("review evidence", bool(findings) and all(f.requirement.strip() and f.evidence.strip() for f in findings),
                 "every reviewed requirement needs concrete evidence")
    from .findings import key as requirement_key
    required = list(dict.fromkeys(getattr(run, "required_requirements", []) or []))
    seen = {requirement_key(f.requirement) for f in findings}
    missing = [r for r in required if requirement_key(r) not in seen]
    report.check("requirement coverage", bool(required) and not missing,
                 f"missing controller requirements: {missing}")
    approved = bool(getattr(envelope, "approved", False))
    scope_paths = getattr(run, "scope_review_paths", None)
    if scope_paths is not None:
        scope_findings = list(getattr(envelope, "scope_findings", []))
        paths = [f.path for f in scope_findings]
        report.check("scope review coverage", set(paths) == set(scope_paths) and len(paths) == len(set(paths))
                     and all(f.reason.strip() for f in scope_findings),
                     "independent review must explain every changed source file exactly once")
        report.check("scope verdict", not approved or all(f.within_scope for f in scope_findings),
                     "approval cannot include out-of-scope changes")
    blocking = list(getattr(envelope, "blocking", []))
    categories = list(getattr(envelope, "blocker_categories", []) or [])
    report.check("blocker categories", not categories or len(categories) == len(blocking),
                 "blocker_categories needs exactly one category per blocking item, in the same order")
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


def tests_pass(argv: list[str]):
    """Gate factory: the given argv command must exit 0.

    argv, never a shell string — same contract as the controller's quality
    checks in `quality.py`. A gate command is configuration, so a shell here
    would turn any configured value into an injection point for no benefit.
    """
    if isinstance(argv, str) or not argv:
        raise ValueError("tests_pass expects a non-empty argv list, not a shell string")
    label = shlex.join(argv)

    def gate(envelope: EnvelopeBase, run) -> GateReport:
        from .utils import operator_env
        result = subprocess.run(argv, cwd=run.repo_root, capture_output=True, text=True, timeout=120,
                                env=operator_env())
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(label, ok, note)
    gate.__name__ = f"tests_pass({label})"
    return gate
