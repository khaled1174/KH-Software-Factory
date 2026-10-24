"""Controller-owned task boundaries; no claim of semantic or OS isolation.

Path checks narrow role permissions; they never replace permissions.enforce.
Meaningful drift inside an allowed file still requires independent review.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .permissions import PermissionBreach, _matches


class ScopeViolation(PermissionBreach):
    """The frozen controller contract was invalid, changed, or exceeded."""


def _nonempty(value: str) -> str:
    if not value.strip():
        raise ValueError("Scope text must not be empty")
    return value


def _relative_pattern(value: str) -> str:
    _nonempty(value)
    if (value.startswith(("/", "~")) or "\\" in value or ":" in value
            or any(ord(char) < 32 for char in value)
            or any(part in ("", ".", "..") for part in value.rstrip("/").split("/"))):
        raise ValueError(f"Expected a safe relative path or pattern: {value!r}")
    return value


def _ordered(values):
    return list(dict.fromkeys(values))


class ScopeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    original_request: str
    deliverables: list[str] = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    allowed_paths: list[str] | None = None
    # Controller-derived from the accepted plan; enforced together with
    # allowed_paths on actual files. Never supplied by the owner's scope file.
    plan_paths: list[str] | None = None
    required_checks: list[Literal["test", "lint", "typecheck", "build"]] = Field(default_factory=list)
    review_required: bool = True

    @field_validator("original_request")
    @classmethod
    def valid_request(cls, value):
        return _nonempty(value)

    @field_validator("deliverables", "non_goals", "constraints")
    @classmethod
    def valid_text_lists(cls, values):
        return [_nonempty(value) for value in values]

    @field_validator("allowed_paths", "plan_paths")
    @classmethod
    def valid_paths(cls, values):
        return None if values is None else [_relative_pattern(value) for value in values]

    def requirements(self) -> list[str]:
        return _ordered([self.original_request, *self.deliverables, *self.constraints,
                         *(f"Out of scope: {item}" for item in self.non_goals)])


@contextmanager
def _parent_fd(path: Path, *, create=False):
    """Walk without following any symlink, including intermediate parents."""
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, path.name
    except OSError as exc:
        raise ScopeViolation(f"Cannot safely access scope state {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _read_json(path: Path):
    with _parent_fd(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ScopeViolation(f"Scope state is not a regular file: {path}")
            return json.load(stream)


def _atomic_json(path: Path, value) -> None:
    with _parent_fd(path, create=True) as (parent, name):
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ScopeViolation(f"Refusing non-regular scope state: {path}")
        temporary = f".{name}.{secrets.token_hex(12)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def load_contract(path: Path | str, request: str | None = None) -> ScopeContract:
    """Load explicit JSON scope. A resumed request must exactly match its origin."""
    contract = ScopeContract.model_validate(_read_json(Path(path)))
    if request is not None and contract.original_request != request:
        raise ScopeViolation("Original request differs from the frozen scope; use a new run")
    return contract


def capture_contract(run, contract: ScopeContract) -> ScopeContract:
    """Freeze scope for this run without allowing resume to widen its boundary."""
    # Revalidate a detached copy: Pydantic frozen does not freeze nested lists.
    contract = ScopeContract.model_validate(contract.model_dump())
    path = Path(run.session_dir) / "scope.json"
    if path.exists() or path.is_symlink():
        existing = load_contract(path, contract.original_request)
        if existing != contract:
            raise ScopeViolation("Scope contract changed for an existing run; use a new run")
    previous = getattr(run, "scope_contract", None)
    if previous is not None and previous != contract:
        raise ScopeViolation("Scope contract cannot change during a run")
    _atomic_json(path, contract.model_dump())
    requirements = _ordered([*contract.requirements(), *getattr(run, "required_requirements", [])])
    _atomic_json(Path(run.session_dir) / "acceptance.json", requirements)
    run.scope_contract = contract
    run.required_requirements = requirements
    return contract


def add_plan_requirements(run, criteria: list[str]) -> list[str]:
    """Plans add acceptance criteria; they cannot replace the owner's request."""
    if not isinstance(criteria, list) or any(not isinstance(item, str) or not item.strip()
                                             for item in criteria):
        raise ScopeViolation("Plan acceptance criteria must be nonempty strings")
    contract = getattr(run, "scope_contract", None)
    baseline = contract.requirements() if contract is not None else []
    requirements = _ordered([*baseline, *getattr(run, "required_requirements", []), *criteria])
    _atomic_json(Path(run.session_dir) / "acceptance.json", requirements)
    run.required_requirements = requirements
    return requirements


def check_paths(contract: ScopeContract, changed_paths: list[str],
                evidence_prefix: str | None = None) -> list[str]:
    """Check content deltas after role checks; caller excludes structural dirs.

    Only the current worker's exact evidence directory is exempt. None inherits
    the role boundary; an empty list authorizes no source-file writes.
    """
    prefix = _relative_pattern(evidence_prefix).rstrip("/") if evidence_prefix else None
    rejected = []
    for path in changed_paths:
        _relative_pattern(path)
        own_evidence = prefix is not None and (path == prefix or path.startswith(prefix + "/"))
        if own_evidence:
            continue
        # Both boundaries apply to the real file: the owner's and the plan's.
        for boundary in (contract.allowed_paths, contract.plan_paths):
            if boundary is not None and not any(_matches(path, pattern) for pattern in boundary):
                rejected.append(path)
                break
    if rejected:
        error = ScopeViolation("Changes exceed task scope: " + ", ".join(rejected))
        error.paths = list(rejected)
        raise error
    return list(changed_paths)


def _wildcard(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern or pattern.endswith("/")


def _contains(owner: str, plan: str) -> bool:
    """Conservative containment: True only when every path matching `plan`
    provably matches `owner`. Anything unproven is False.

    Accepted proofs: identical patterns; `owner` is `**`; `owner` is a
    directory prefix (`dir/`) or `dir/**` and `plan` lies under `dir/`.
    Comparing a plan *pattern* against an owner pattern as if it were a file
    name is not a proof — `src/*.py` "matches" `src/?.py` that way while
    describing far more files.
    """
    if owner == plan or owner == "**":
        return True
    if owner.endswith("/**"):
        prefix = owner[:-2]
    elif owner.endswith("/"):
        prefix = owner
    else:
        return False
    if "*" in prefix or "?" in prefix:
        return False
    return plan.startswith(prefix) or plan + "/" == prefix


def plan_boundary(contract: ScopeContract, proposed: list[str]) -> list[str]:
    """Validate the planner's proposed paths against the owner's contract.

    Owner scope wins: a plan may narrow it, never widen it. Concrete plan
    paths must match an owner pattern; plan patterns must be provably
    contained in one. Without an owner boundary the plan must supply one, so
    guarded startup is default-deny. Runtime enforcement still checks real
    files against both boundaries (see check_paths).
    """
    if not isinstance(proposed, list) or not proposed or any(not isinstance(p, str) for p in proposed):
        raise ScopeViolation("Plan must declare at least one allowed source path or pattern")
    proposed = _ordered(_relative_pattern(p) for p in proposed)
    if contract.allowed_paths is not None:
        outside = []
        for plan in proposed:
            if _wildcard(plan):
                ok = any(_contains(owner, plan) for owner in contract.allowed_paths)
            else:
                ok = any(_matches(plan, owner) for owner in contract.allowed_paths)
            if not ok:
                outside.append(plan)
        if outside:
            raise ScopeViolation("Plan proposes paths outside the owner scope (or patterns whose containment "
                                 "cannot be proven; use concrete files or a pattern under an owner directory): "
                                 + ", ".join(outside))
    return proposed


def effective_contract(run, contract: ScopeContract, proposed: list[str]) -> ScopeContract:
    """Freeze the builder's effective boundary for this run.

    scope.json keeps the owner's original contract untouched. effective_scope.json
    records what is enforced on source writes: the owner's allowed_paths (or the
    plan's, when the owner gave none) AND the plan's paths — a file must satisfy
    both.
    """
    narrowed = plan_boundary(contract, proposed)
    effective = ScopeContract.model_validate({**contract.model_dump(),
                                              "allowed_paths": contract.allowed_paths if contract.allowed_paths is not None else narrowed,
                                              "plan_paths": narrowed})
    path = Path(run.session_dir) / "effective_scope.json"
    if path.exists() or path.is_symlink():
        existing = load_contract(path, contract.original_request)
        if existing != effective:
            raise ScopeViolation("Effective scope changed for an existing run; use a new run")
    _atomic_json(path, effective.model_dump())
    return effective


def activate_revised_contract(run, owner_contract, plan, approval, context_packet) -> ScopeContract:
    """Controller activates only an independently approved, recorded plan revision."""
    from . import agents, permissions, planning
    planning.assert_plan_approval(run, plan, approval, owner_contract, context_packet)
    if not approval.parent_approval_digest or approval.parent_approval_version < 1:
        raise ScopeViolation('Effective scope revision requires recorded approval lineage')
    if load_contract(Path(run.session_dir) / 'scope.json') != owner_contract:
        raise ScopeViolation('Owner scope changed before activating the reviewed revision')
    parent = _read_json(Path(run.session_dir) / 'planning' / f'approval-{approval.parent_approval_version}.json')
    if planning.digest(parent) != approval.parent_approval_digest:
        raise ScopeViolation('Parent plan approval differs from the revision lineage')
    prior = planning.ReviewedPlanOutput.model_validate(_read_json(
        Path(run.session_dir) / 'planning' / f'plan-{approval.parent_approval_version}.json'))
    if planning.digest(prior) != parent['plan_digest']:
        raise ScopeViolation('Previous plan differs from the revision lineage')
    narrowed = plan_boundary(owner_contract, plan.allowed_paths)
    builder = agents.resolve(run.cfg, 'builder')
    if any(not permissions.permitted(path, builder, run.cfg) for path in approval.revision_paths):
        raise ScopeViolation('Reviewed revision names protected or role-prohibited paths')
    before = ScopeContract.model_validate({**owner_contract.model_dump(),
        'allowed_paths': owner_contract.allowed_paths if owner_contract.allowed_paths is not None else prior.allowed_paths,
        'plan_paths': prior.allowed_paths})
    after = ScopeContract.model_validate({**owner_contract.model_dump(),
        'allowed_paths': owner_contract.allowed_paths if owner_contract.allowed_paths is not None else narrowed,
        'plan_paths': narrowed})
    current = load_contract(Path(run.session_dir) / 'effective_scope.json')
    if current not in (before, after):
        raise ScopeViolation('Current effective scope differs from the approved revision parent')
    record = {'schema_version': 1, 'version': approval.version, 'reason': approval.revision_reason,
              'parent_approval_digest': approval.parent_approval_digest,
              'approval_digest': planning.digest(approval), 'plan_digest': approval.plan_digest,
              'candidate_digest': approval.revision_candidate_digest,
              'before': before.model_dump(), 'after': after.model_dump()}
    history = Path(run.session_dir) / 'effective_scope-revisions' / f'{approval.version}.json'
    if history.exists() or history.is_symlink():
        if _read_json(history) != record:
            raise ScopeViolation('Effective scope revision history cannot be replaced')
    else:
        _atomic_json(history, record)
    _atomic_json(Path(run.session_dir) / 'effective_scope.json', after.model_dump())
    return after
