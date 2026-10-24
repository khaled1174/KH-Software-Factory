"""Concrete data types for the KH ADW system.

RULE (four-param rule): any function that takes more than 4 parameters takes
ONE of these objects instead. AgentCall and PhaseParams are the pattern.

Every agent call declares a concrete output type — an EnvelopeBase subclass —
that its final JSON response is parsed against. No untyped handoffs.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

RUNTIMES = {"claude_code": ("claude",), "delegate_codex": ("node", "codex")}
CodingAgent = Literal[tuple(RUNTIMES)]

PhaseKind = Literal["engineer", "agent", "code"]
PhaseStatus = Literal["queued", "running", "success", "fail"]


# ── Phases ────────────────────────────────────────────────────────────────────

class PhaseParams(BaseModel):
    """Everything run.phase() needs. Passed as one object, never loose params."""

    name: str                       # short id, unique within the run: "plan", "build"
    kind: PhaseKind                 # which lane the block renders in
    owner: str                      # engineer's name, "git", or an agent name from config
    description: str                # REQUIRED: what this phase does and why — see below
    retries: int = 0                # agent phases: gate-failure retries via continue

    @field_validator("description")
    @classmethod
    def _description_must_be_earned(cls, value: str, info: ValidationInfo) -> str:
        """A phase name identifies; a description explains. Both are required.

        The description is the only sentence the trace, the console, and the
        phase block in the UI ever show about intent — everything else is ids,
        statuses, and timings. `commit_plan: "Commit the plan"` tells a reader
        nothing they could not already see, so an echo is rejected the same way
        a blank one is. This is a construction-time error on purpose: it fires
        before the phase opens, not after a run is already in the trace.
        """
        text = " ".join(value.split())
        name = str(info.data.get("name", "?"))
        if not text:
            raise ValueError(
                f"phase {name!r}: description is required — one sentence on what this "
                f"phase does and why. It is what the trace and the UI show.")
        if text.rstrip(".").casefold() == name.replace("_", " ").casefold():
            raise ValueError(
                f"phase {name!r}: description {text!r} only restates the phase name — "
                f"say what it does and why instead.")
        return text


class Phase(BaseModel):
    """The persisted phase record — PhaseParams plus lifecycle."""

    phase_id: str
    adw_id: str
    seq: int
    params: PhaseParams
    status: PhaseStatus = "fail"    # success must be earned
    attempt: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Envelopes (agent output types) ───────────────────────────────────────────

class ArtifactDeclaration(BaseModel):
    """Typed metadata for one declared evidence file (SYS-07).

    `artifacts` stays the list of paths every role already returns; a
    declaration adds what the gate cannot infer from a path: what the file is,
    which base a relative path resolves against, and whether an empty file is
    legitimate. A raw stream may be empty only when the controller's command
    record for `invocation_id` also recorded an empty stream.
    """
    model_config = ConfigDict(extra="forbid")
    path: str
    kind: Literal["report", "raw_stream", "data", "diff", "other"] = "report"
    base: Literal["evidence", "repo"] = "repo"
    invocation_id: str = ""
    producing_phase: str = ""
    sha256: str = ""
    bytes: Optional[int] = Field(default=None, ge=0)


class EnvelopeBase(BaseModel):
    """Base of every agent's final JSON response. Output types extend this."""

    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    artifact_declarations: list[ArtifactDeclaration] = Field(default_factory=list)
    notes_for_next_agent: str = ""
    observations: list[dict[str, Any]] = Field(default_factory=list)


class GenericOutput(EnvelopeBase):
    pass


def _literal_test_commands(values):
    if any(not value.strip() or any(char in value for char in ("\x00", "\n", "\r")) for value in values):
        raise ValueError("required_test_commands must contain nonempty literal single-line commands")
    return values


class PlanOutput(EnvelopeBase):
    acceptance_criteria: list[str] = Field(default_factory=list)
    # Source paths/patterns the plan needs. In guarded startup the controller
    # freezes these as the builder's boundary when the owner supplied none; when
    # the owner did supply allowed_paths, the plan may only narrow them.
    allowed_paths: list[str] = Field(default_factory=list)
    # Approved plan requirements never add native permissions or shell grants.
    required_test_commands: list[str] = Field(default_factory=list)

    @field_validator("required_test_commands")
    @classmethod
    def literal_test_commands(cls, values):
        return _literal_test_commands(values)
    # Subject for committing the PLAN — the spec file the planner wrote, not the
    # implementation it describes. Each agent's commit_message covers its own
    # work product, so a chain that commits per step never reuses one agent's
    # words for another agent's diff.
    commit_message: str = ""


class BuildOutput(EnvelopeBase):
    # THIS phase's change set, checked exactly by diff_matches_claims.
    changed_files: list[str] = Field(default_factory=list)
    # Optional: the whole task's change set so far (all phases, resumed work
    # included). Checked against the controller's task delta when supplied, and
    # never confused with changed_files (SYS-07).
    cumulative_task_changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""        # consumed by the git commit phase


class ScoutFinding(BaseModel):
    file: str
    note: str = ""


class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    """One thing the request (or plan) asked for, and whether it is there."""

    requirement: str                # the ask, in the requester's words
    met: bool
    evidence: str = ""              # where it lives, or what is missing


class ScopeFinding(BaseModel):
    path: str
    within_scope: bool
    reason: str


class ReviewOutput(EnvelopeBase):
    """Confirmation that what was built is what was asked for — not a test run."""

    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)   # what must change before approval
    # One action per blocking item, same order (FIX-03): code_change (builder
    # repairs source), evidence_delivery (controller supplies existing records,
    # unchanged candidate is reviewed again), historical_evidence_unavailable and
    # owner_decision (an owner decision is drafted; the run stops with the
    # candidate kept), infrastructure_or_provider (no builder round can fix it).
    # Omitted = every item is a code_change.
    blocker_categories: list[Literal["code_change", "evidence_delivery", "historical_evidence_unavailable",
                                     "owner_decision", "infrastructure_or_provider"]] = Field(default_factory=list)
    scope_findings: list[ScopeFinding] = Field(default_factory=list)
    # Concrete files required by a rejected review. This requests reviewed
    # replanning, never permission for the builder to widen its own boundary.
    repair_paths: list[str] = Field(default_factory=list)


class DocumentOutput(EnvelopeBase):
    """Where the write-up of a completed change landed."""

    document_path: str = ""         # the doc in the repo, e.g. app_docs/<adw_id>_<slug>.md
    documented_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


# ── Deterministic quality blocks ─────────────────────────────────────────────

QualityArea = Literal["frontend", "backend"]
QualityOperation = Literal["lint", "typecheck", "build"]


class QualityCheckSpec(BaseModel):
    """One deterministic quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    argv: list[str]
    timeout_seconds: int = 120


class QualityCheckResult(BaseModel):
    """Captured evidence from one quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    command: str
    returncode: int
    passed: bool
    duration_seconds: float
    output_artifact: str
    # The tail of stdout+stderr, verbatim and unparsed. A failure has to travel
    # back to the builder as an envelope, and the builder cannot open a log file
    # it was never handed — so the evidence rides along. Deliberately raw: every
    # runner formats failures differently and a generic parser would be
    # confidently wrong. The full log is always at output_artifact.
    output_tail: str = ""
    # Set when a review-only continuation reused this result for the same
    # candidate bytes instead of running the command again (SYS-10).
    reused_from_run: str = ""
    candidate_sha256: str = ""


class QualityResult(BaseModel):
    """Aggregate result from a quality block: every check it ran, and the verdict."""

    passed: bool
    checks: list[QualityCheckResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# ── Change capture (git diff, deterministic) ─────────────────────────────────

class ChangeCapture(BaseModel):
    """Everything documentation.capture() needs. One object, never loose params."""

    base: str = "main"              # the ref the work is measured against
    max_diff_lines: int = 2000      # the diff artifact is truncated past this
    include_untracked: bool = True  # a brand-new file is part of the change


class BaseRef(BaseModel):
    """The commit a change is measured from, and why that one.

    `reason` is the line the trace shows. A diff is only as trustworthy as the
    thing it was taken against, so the ADW records that choice instead of
    leaving the reader to infer it.
    """

    ref: str                        # what was asked for: "main", or a pinned sha
    commit: str                     # the commit actually diffed against
    reason: str = ""

    @property
    def label(self) -> str:
        """Display form — a named ref as itself, a pinned raw sha shortened."""
        if len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref):
            return self.ref[:7]
        return self.ref


class ChangeSet(BaseModel):
    """What changed since the base commit — pure git facts, no judgement."""

    base: BaseRef
    files: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""                  # `git diff --stat` output, verbatim
    diff_path: str = ""             # the full diff, written into context_handoff/
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not (self.files or self.untracked)


class ChangesOutput(EnvelopeBase):
    """A ChangeSet shaped as an envelope so an agent can be handed it directly.

    Same adapter idea as VerifyOutput: code computes the diff, the documenter
    consumes it through the one door every agent handoff uses.
    """

    base: str = ""                  # "<ref> @ <commit> — <reason>"
    changed_files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    diff_path: str = ""             # read this for the full diff


class VerifyOutput(EnvelopeBase):
    """A deterministic result, shaped as an envelope so an agent can consume it.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door a tester agent's report used to —
    the ADW script is the only thing that knows the difference.
    """

    passed: bool = False
    failures: list[str] = Field(default_factory=list)


# ── Agent calls ──────────────────────────────────────────────────────────────

class GateCheck(BaseModel):
    """One thing a gate looked at, and what it found.

    `note` is the evidence — "exists, 2.1KB", "exit 0", "not in the diff". On a
    failed check it doubles as the reason, so it is what the agent is told.
    """

    item: str                       # what was checked: a path, a command, a test
    ok: bool
    note: str = ""


class GateReport(BaseModel):
    """What every gate returns: the checks it ran. Violations are derived.

    Authoring stays a one-liner per item — `report.check(...)` appends and
    returns self, so a gate is a loop and a return.
    """

    checks: list[GateCheck] = Field(default_factory=list)

    def check(self, item: str, ok: bool, note: str = "") -> "GateReport":
        self.checks.append(GateCheck(item=item, ok=ok, note=note))
        return self

    @property
    def violations(self) -> list[str]:
        return [f"{c.item}: {c.note or 'failed'}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return not self.violations


class AgentCall(BaseModel):
    """One agent invocation: prompt in, typed envelope out, gates verified."""

    model_config = {"arbitrary_types_allowed": True}

    output_type: Type[EnvelopeBase]
    prompt: str
    previous: Optional[EnvelopeBase] = None
    # The accepted plan, re-sent on every build/repair attempt so a correction
    # loop never loses the original implementation contract.
    plan: Optional[EnvelopeBase] = None
    gates: list[Callable] = Field(default_factory=list)   # gate(envelope, run) -> list[str]


# ── Config ───────────────────────────────────────────────────────────────────

class PromptEngineering(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: str                     # path to system.md
    user: str                       # path to user.md


class RuntimeCapabilityProbe(BaseModel):
    """Retired probe schema, accepted without effect for one compatibility release."""
    model_config = ConfigDict(extra="forbid", strict=True)
    capability: Literal["shell", "swift", "database"]
    argv: list[str] = Field(min_length=1)
    expect_stdout: str
    timeout_seconds: int = Field(default=15, ge=1, le=60)
    read_only: Literal[True] = True

    @field_validator("argv")
    @classmethod
    def nonblank_argv(cls, values):
        if any(not value.strip() or "\x00" in value for value in values):
            raise ValueError("runtime probe argv must contain nonblank strings without NUL")
        return values


def supported_runtime(value):
    if value in ("antigravity_cli", "gemini_cli", "pi"):
        raise ValueError(f"{value} is retired; select a supported runtime in the configuration "
                         "and rerun /kh-setup-models. No automatic conversion is permitted.")
    return value


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    # One send of a high-effort planner or a first dependency build routinely
    # passes five minutes; 300 s killed real plans mid-write (exit 124).
    timeout_seconds: int = Field(default=1800, gt=0)
    delegate_relay_path: Optional[str] = None
    delegate_sandbox: Literal["read-only", "workspace-write"] = "workspace-write"
    coding_agent: CodingAgent = "claude_code"
    _supported_runtime = field_validator("coding_agent", mode="before")(supported_runtime)
    model: str = "sonnet"
    thinking: str = "medium"        # off | minimal | low | medium | high | xhigh | max
    supported_thinking: Optional[list[str]] = None  # explicit local capability declaration
    color: str = ""                 # hex swatch for this agent's lane in the UI
    purpose: str = ""
    prompt_engineering: PromptEngineering
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # allowlist; None = all tools usable
    # What this agent may MODIFY in the repo, enforced in code after every call
    # (see adw_modules/permissions.py). `tools` cannot express this: `bash` runs
    # anything and `write` reaches any path, so an agent's capability list is a
    # statement of intent that nothing checks.
    #   None  -> unrestricted, except the roster-wide `protected_files` paths
    #   []    -> no source promotion, including untracked and ignored source files
    #   [...] -> only these. A trailing "/" means a directory prefix; a "*"
    #            makes it a glob; anything else is an exact path.
    writes: Optional[list[str]] = None
    # Requirements describe this role's work, never a model ranking or an
    # approval grant. Legacy Swift/database requirements check shell configuration
    # only; native permission, executable and resource access remain unverified.
    required_capabilities: list[Literal["source_write", "shell", "swift", "database"]] = Field(default_factory=list)
    capability_probes: list[RuntimeCapabilityProbe] = Field(default_factory=list)


class VerificationAdapter(BaseModel):
    """Owner-configured evidence-producing command; no shell interpolation."""
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    kind: Literal["visual", "interaction", "e2e"]
    applicable_paths: list[str] = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=120, gt=0, le=3600)

    @field_validator("applicable_paths", "argv")
    @classmethod
    def nonblank_entries(cls, values):
        if any(not value.strip() or "\x00" in value for value in values):
            raise ValueError("adapter entries cannot be blank or contain NUL")
        return values

    @field_validator("applicable_paths")
    @classmethod
    def relative_patterns(cls, values):
        if any(value.startswith(("/", "~")) or "\\" in value or ":" in value
               or any(part in ("", ".", "..") for part in value.rstrip("/").split("/"))
               for value in values):
            raise ValueError("adapter patterns must be safe relative paths")
        return values


class ContextGuardConfig(BaseModel):
    """When a repair loop may no longer continue in the same session.

    Corrections re-enter the same coding-agent session, so occupancy grows with
    every repair. `rotate_at_percent` is measured against the model's declared
    window; a model whose window is unknown is never judged.
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    rotate_at_percent: float = Field(default=75.0, gt=0, le=95)
    # A fresh session still has to hold the task prompt. When even that crosses
    # the line, rotating again only buys another send, so the phase fails after
    # this many rotations and says the task itself does not fit.
    max_rotations: int = Field(default=2, ge=0, le=10)
    # Roles whose verdict depends on having read the evidence themselves. A
    # reconstruction is not evidence, so these fail instead of rotating.
    never_rotate: list[str] = Field(default_factory=lambda: ["reviewer", "plan_reviewer"])


class ConfigDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_guard: ContextGuardConfig = Field(default_factory=ContextGuardConfig)
    auto_commit: bool = False  # Accepted for compatibility; execution never commits.
    writes: Optional[list[str]] = None
    required_quality_checks: list[Literal["test", "lint", "typecheck", "build"]] = Field(default_factory=lambda: ["test", "lint", "typecheck", "build"])
    required_test_commands: list[str] = Field(default_factory=list)
    test_environment_overrides: dict[Literal["DATABASE_URL", "TEST_DATABASE_URL"], list[str]] = Field(default_factory=dict)

    @field_validator("required_test_commands")
    @classmethod
    def literal_test_commands(cls, values):
        return _literal_test_commands(values)

    @field_validator("test_environment_overrides")
    @classmethod
    def literal_database_overrides(cls, values):
        from .command_grants import validate_overrides
        return validate_overrides(values)

    snapshot_max_bytes: int = Field(default=536870912, gt=0)
    # Untracked disposable directories only. Tracked files are never excluded.
    generated_paths: list[str] = Field(default_factory=lambda: [
        "**/.next/", "**/dist/", "**/coverage/",
        "**/__pycache__/", "**/.pytest_cache/", "**/.mypy_cache/",
        "**/.ruff_cache/", "**/.turbo/", "**/.expo/", "**/.venv/",
        # Toolchain build output whose directory name is never source: SwiftPM
        # (`swift build`/`swift test`), Gradle, Dart/Flutter. Plain `build/` and
        # `target/` stay out: projects keep source there, and an untracked new
        # directory matching a pattern is never promoted.
        "**/.build/", "**/.swiftpm/", "**/.gradle/", "**/.dart_tool/",
    ])

    dependency_paths: list[str] = Field(default_factory=lambda: ["**/node_modules/"])

    @field_validator("generated_paths", "dependency_paths")
    @classmethod
    def generated_patterns(cls, values):
        for value in values:
            parts = value.rstrip("/").split("/")
            if (not value.endswith("/") or value.startswith(("/", "~"))
                    or any(p in ("", ".", "..") for p in parts)
                    or any(c in value for c in ("\\", ":"))
                    or any(ord(c) < 32 for c in value)
                    or not parts[-1].replace("-", "").replace("_", "").replace(".", "").isalnum()
                    or parts[-1] in (".git", ".kh", ".claude", ".kh-observer")):
                raise ValueError("generated_paths/dependency_paths must name safe directory patterns with a concrete final directory")
        return values
    verification_adapters: list[VerificationAdapter] = Field(default_factory=list)

    @field_validator("verification_adapters")
    @classmethod
    def unique_adapters(cls, values):
        if len({value.name for value in values}) != len(values):
            raise ValueError("verification adapter names must be unique")
        return values

    # Which .env-sourced variables reach agents and quality commands. Unset
    # passes all of them, keeping an existing
    # provider login working on upgrade. A list passes only the named ones;
    # [] passes none. Variables the operator exported in their own shell are
    # never affected — this bounds the project file, not the environment.
    env_allowlist: Optional[list[str]] = None

    @field_validator("env_allowlist")
    @classmethod
    def declared_variable_names(cls, values):
        if values is None:
            return values
        for value in values:
            if not value.strip() or value != value.strip() or any(
                    c.isspace() or c == "=" or ord(c) < 32 for c in value):
                raise ValueError("env_allowlist entries must be bare variable names")
        if len(set(values)) != len(values):
            raise ValueError("env_allowlist entries must be unique")
        return values

    quality_checks: dict[str, list[str]] = Field(default_factory=dict)
    coding_agent: CodingAgent = "claude_code"
    _supported_runtime = field_validator("coding_agent", mode="before")(supported_runtime)
    model: str = "sonnet"
    thinking: str = "medium"
    color: str = ""
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # roster-wide allowlist; None = all tools usable
    # Controller-owned paths cannot be unlocked by an agent writes allowlist.
    # The factory's own code is the default: an agent must not be able to edit
    # the machinery that decides whether its work passed.
    protected_files: list[str] = Field(default_factory=lambda: [
        "adws/adw_modules/", "adws/adw_kh_config/", "adws/adw_*.py",
    ])
    data_dir: str = "adws/adw_data"


class ObservabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    db: str = "adws/adw_data/kh.db"
    poll_ms: int = 500


class ReviewConfig(BaseModel):
    """Retired panel setting, accepted without effect for one compatibility release."""

    model_config = ConfigDict(extra="forbid")
    panel: list[str] = Field(default_factory=list)


class JevScoutSettings(BaseModel):
    """Retired setting accepted without effect for one compatibility release."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)


def _owner_review_globs(values):
    """Root-relative POSIX globs that can actually match a changed path."""
    for value in values:
        parts = value.split("/")
        if value.endswith("/"):
            parts = parts[:-1]
        if (not value or value.startswith(("/", "~")) or "\\" in value
                or any(p in ("", ".", "..") for p in parts) or any(ord(c) < 32 for c in value)):
            raise ValueError("owner_review paths must be root-relative POSIX globs "
                             "(no leading ./ or /, no . or .. segments, no backslashes)")
    return values


class OwnerReviewCategory(BaseModel):
    """One owner-held category: changed-path globs and added-line keywords."""
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def relative_globs(cls, values):
        return _owner_review_globs(values)

    @field_validator("keywords")
    @classmethod
    def lowercase_keywords(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("owner_review keywords cannot be blank")
        return [value.lower() for value in values]


def _default_owner_categories():
    return {
        "money": OwnerReviewCategory(
            paths=["**/billing/**", "**/payments/**", "**/stripe/**", "**/checkout/**"],
            keywords=["stripe.", "payment_intent", "price_id", "invoice.create"]),
        "auth": OwnerReviewCategory(
            paths=["**/auth/**", "**/authz/**", "**/permissions/**"],
            keywords=["jwt.sign", "bcrypt", "password_hash", "service_role"]),
        "schema": OwnerReviewCategory(
            keywords=["drop table", "drop column", "truncate table", "rename column", "drop schema"]),
    }


class OwnerReviewSettings(BaseModel):
    """Sensitive categories in the verified delta, for the owner's attention.

    `report` (default): each category hit becomes a requirement the independent
    reviewer must rule on before approval; the task then promotes and closes
    itself, and the hits are listed in its receipt and task report for the
    owner to read afterwards. `hold`: a hit stops promotion until the accepted
    intent approves the category (`Approved sensitive changes`) — for owners who
    want a human in the loop. `size` is the built-in category for more than
    max_changed_lines added+removed lines outside size_exclude; 0 disables it.
    Setting `categories` replaces the defaults; `{}` keeps only `size`.
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    mode: Literal["report", "hold"] = "report"
    max_changed_lines: int = Field(default=400, ge=0, le=1_000_000, strict=True)
    size_exclude: list[str] = Field(default_factory=lambda: [
        "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml", "**/bun.lock", "**/bun.lockb",
        "**/npm-shrinkwrap.json", "**/Cargo.lock", "**/poetry.lock", "**/uv.lock", "**/Pipfile.lock",
        "**/Gemfile.lock", "**/composer.lock", "**/go.sum",
        "**/generated/**", "**/__generated__/**", "**/*.generated.*", "**/*.min.js", "**/*.min.css",
        "**/*.map", "**/__snapshots__/**", "**/*.snap",
    ])
    categories: dict[str, OwnerReviewCategory] = Field(default_factory=_default_owner_categories)

    @field_validator("categories")
    @classmethod
    def category_names(cls, values):
        for name in values:
            if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name)) or name == "size":
                raise ValueError("owner_review category names are lowercase identifiers; 'size' is built in")
        return values

    @field_validator("size_exclude")
    @classmethod
    def size_globs(cls, values):
        return _owner_review_globs(values)


class FloorGuardSettings(BaseModel):
    """Bar-lowering moves in the verified delta become reviewer requirements (adw_modules/floor_guard.py)."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)


class CommandClass(BaseModel):
    """One owner-defined command class. Classification is by the command text the
    controller recorded, never by a label an agent gives it (SYS-01/SYS-09)."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    # strict: the record must carry an authentic exit status and the complete,
    # unfiltered output. Not strict: exact command and timing are always
    # recorded, and a filtered display is kept as a labelled excerpt.
    strict: bool = True
    patterns: list[str] = Field(min_length=1)

    @field_validator("patterns")
    @classmethod
    def patterns_compile(cls, values):
        for value in values:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"invalid command pattern {value!r}: {error}") from error
        return values


def _default_command_classes() -> list[CommandClass]:
    return [CommandClass(**c) for c in (
        {"name": "test", "strict": True, "patterns": [
            r"^(swift|cargo|go|dotnet|bun|deno|mix|flutter|dart) test\b", r"^(pytest|jest|vitest|rspec|phpunit|ctest)\b",
            r"^python[0-9.]* -m (pytest|unittest)\b", r"^(uv|poetry|pipenv) run (pytest|python[0-9.]* -m (pytest|unittest))\b",
            r"^(npm|pnpm|yarn) (run )?test\b", r"^npx (jest|vitest)\b", r"^xcodebuild\b.*\btest(-without-building)?\b",
            r"^make (test|check)\b", r"^just test\b"]},
        {"name": "format", "strict": True, "patterns": [
            r"^(swift-format|swiftformat|prettier|black|gofmt|goimports|rustfmt|clang-format|isort|shfmt)\b",
            r"^swift format\b", r"^ruff format\b", r"^cargo fmt\b", r"^(npx|bunx) prettier\b"]},
        {"name": "lint", "strict": True, "patterns": [
            r"^(swiftlint|eslint|flake8|pylint|mypy|pyright|golangci-lint|shellcheck|hadolint)\b",
            r"^ruff( check)?\b", r"^cargo clippy\b", r"^(npx|bunx) (eslint|tsc)\b", r"^tsc\b.*--noEmit\b"]},
        {"name": "probe", "strict": True, "patterns": [
            r"^(curl|wget|psql|pg_isready|redis-cli|grpcurl|http|xh|nc)\b"]},
        {"name": "exploratory_compile", "strict": False, "patterns": [
            r"^(swift|cargo|go|dotnet) build\b", r"^swiftc\b", r"^xcodebuild\b", r"^(make|cmake|ninja|gradle|mvn)\b",
            r"^(npm|pnpm|yarn|bun) run build\b", r"^tsc\b"]},
    )]


class CommandPolicySettings(BaseModel):
    """How the controller classifies and judges the commands a coding agent ran (SYS-01)."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    # Ordered: the first class whose pattern matches a command stage wins.
    classes: list[CommandClass] = Field(default_factory=_default_command_classes)
    # A builder whose strict command record is incomplete (status hidden by a
    # pipeline, output cut by tail/grep, interrupted) gets the gap named in the
    # same session while it can still re-run the command (SYS-01 early detection).
    require_complete_strict_records: bool = Field(default=True, strict=True)

    @field_validator("classes")
    @classmethod
    def unique_names(cls, values):
        names = [c.name for c in values]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate command class names: {names}")
        return values


class CapabilityCheck(BaseModel):
    """A controller-run probe that the worker environment really lacks a capability.

    Runs with the coding agents' environment before planning; the task refuses
    to start unless stdout (stripped) equals `expect_stdout`. Example: the test
    database role is not a superuser:
    argv: [psql, "${DATABASE_URL}", -tAc, "select rolsuper from pg_roles where rolname = current_user"]
    expect_stdout: "f"
    """
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    argv: list[str] = Field(min_length=1)
    expect_stdout: str
    timeout_seconds: int = Field(default=30, ge=1, le=600)


def _default_forbidden_commands() -> list[str]:
    # SQL that changes server-wide state, wherever it appears (psql -c, a file
    # piped to psql, ...). Container and service control is judged structurally
    # (executable and subcommand of each command stage) in infrastructure.py, so
    # `cat docker/start.sh` or `docker run --rm` are not mistaken for it.
    return [r"(?i)\balter\s+system\s+(set|reset)\b", r"(?i)\bpg_reload_conf\s*\("]


class InfrastructureSettings(BaseModel):
    """Infrastructure capabilities, kept separate from file permissions (SYS-05).

    File scope only sees files; a coding agent that restarts a shared database
    container changes no file. These settings shape what agents can reach
    (environment, PATH, Claude Code deny rules), probe it before the task, and
    stop the run when an agent executes a forbidden infrastructure command.
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    # Regexes over every command an agent runs (absolute paths and `sh -c`
    # wrappers included). An executed match stops the phase; a refused attempt
    # is recorded without claiming any server state changed.
    forbidden_commands: list[str] = Field(default_factory=_default_forbidden_commands)
    # Executables replaced on the agents' PATH by a refusing stub, and denied
    # to Claude Code roles by permission rules. Detection above also covers
    # absolute paths that bypass PATH.
    # Empty by default: many test suites legitimately run `docker compose up`.
    deny_executables: list[str] = Field(default_factory=list)
    # Container/service subcommands that change shared state. An executed one
    # stops the phase; the container read/start commands a test suite needs
    # (ps, logs, exec, run, compose up) are not in the list.
    forbidden_container_actions: list[str] = Field(default_factory=lambda: [
        "restart", "stop", "kill", "rm", "down", "pause", "update", "prune"])
    # Environment the agents (not the controller's checks) receive: names to
    # remove (an admin database URL, DOCKER_HOST) and values to set (a
    # non-superuser DATABASE_URL). ${NAME} expands from the operator's env.
    withhold_env: list[str] = Field(default_factory=list)
    worker_env: dict[str, str] = Field(default_factory=dict)
    capability_checks: list[CapabilityCheck] = Field(default_factory=list)
    # When set, agents get KH_RESOURCE_PREFIX=<prefix><run>_ and a DROP
    # SCHEMA/DATABASE of anything not starting with it is forbidden.
    resource_prefix: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,30}$")

    @field_validator("forbidden_commands")
    @classmethod
    def forbidden_compile(cls, values):
        for value in values:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"invalid forbidden command pattern {value!r}: {error}") from error
        return values

    @field_validator("deny_executables")
    @classmethod
    def plain_names(cls, values):
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9._+-]+", value):
                raise ValueError(f"deny_executables entries are bare executable names: {value!r}")
        return values


class DocumentationSettings(BaseModel):
    """Legacy omission stays compatible; generated project config enables this stage."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)


class ExecutionSettings(BaseModel):
    """Optional owner limits; null selects the workflow-specific default."""
    model_config = ConfigDict(extra="forbid")
    task_kind: Literal['generic', 'bug', 'feature', 'maintenance'] = 'generic'
    documentation: DocumentationSettings = Field(default_factory=DocumentationSettings)
    command_policy: CommandPolicySettings = Field(default_factory=CommandPolicySettings)
    infrastructure: InfrastructureSettings = Field(default_factory=InfrastructureSettings)
    # Before a run: compare the configured deadline with the time the
    # permitted rounds have actually taken (warn: report it; refuse: do not
    # start). During a run: never begin a build or a review that the remaining
    # time cannot finish (SYS-04).
    timing_preflight: Literal["warn", "refuse", "off"] = "warn"
    # A rejection whose every blocker is evidence_delivery gets this many
    # re-reviews of the unchanged candidate after the controller re-verifies and
    # indexes its records; it consumes no repair round, only model sends (FIX-03).
    evidence_review_rounds: int = Field(default=1, ge=0, le=3, strict=True)
    max_model_sends: Optional[int] = Field(default=None, ge=4, le=1000, strict=True)
    # One gate correction per guarded agent phase (architect, context review,
    # planner, plan review, builder, reviewer) needs room beyond the old 2.
    max_output_corrections: int = Field(default=6, ge=0, le=20, strict=True)
    deadline_seconds: Optional[int] = Field(default=None, ge=1, le=86400, strict=True)
    # A send that fails on a provider outage (overloaded, at capacity, 5xx,
    # dropped connection) is repeated in the same session after a pause of
    # delay, 2x delay, 4x delay ... up to this many times per send. Retries do
    # not spend model-send or correction budget; the run deadline still bounds
    # them. 0 turns the retry off.
    transient_retries: int = Field(default=3, ge=0, le=10, strict=True)
    transient_retry_delay_seconds: int = Field(default=60, ge=1, le=1800, strict=True)
    # A re-run of a task whose request, scope, project context, planner and
    # plan-reviewer settings and source are all unchanged reuses the plan an
    # earlier run had approved instead of planning and reviewing it again.
    reuse_approved_plan: bool = True
    # A task run that stops before acceptance keeps its unaccepted work in
    # .kh/work/<task>/ (never in the project source); the next run of the
    # same task, on the same project tree and plan, continues it.
    resume_failed_builds: bool = True
    # Full workflows may continue a safely interrupted builder or a repair
    # window that made progress. All continuations share the original deadline
    # and send counters. Zero preserves the original bounded stop behavior.
    recovery_rounds: int = Field(default=2, ge=0, le=5, strict=True)
    light_max_files: int = Field(default=8, ge=1, le=50, strict=True)
    light_risk_paths: list[str] = Field(default_factory=list)
    jev_scout: JevScoutSettings = Field(default_factory=JevScoutSettings)
    owner_review: OwnerReviewSettings = Field(default_factory=OwnerReviewSettings)
    floor_guard: FloorGuardSettings = Field(default_factory=FloorGuardSettings)


class KHConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    defaults: ConfigDefaults = Field(default_factory=ConfigDefaults)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    agents: list[AgentConfig] = Field(default_factory=list)
    routing: dict[str, Any] = Field(default_factory=dict)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)


# ── Tracing ──────────────────────────────────────────────────────────────────

class EventRecord(BaseModel):
    """One traced event, always logged against adw_id + phase."""

    adw_id: str
    phase_id: str = ""
    type: str                       # phase_start | agent_start | tool_call | handoff | gate_pass | gate_fail | log | agent_end | phase_end | error
    name: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: str = ""
    tokens: Optional[int] = None
    # Spans: set both when an event covers real elapsed time (a tool call), so
    # the UI lays it out on a time axis without parsing payload JSON. Left unset,
    # the tracer stamps started_at with the moment the event was recorded.
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Pi coding agent interface ────────────────────────────────────────────────



class CCRequest(BaseModel):
    timeout_seconds: int = Field(default=300, gt=0)
    """Everything one non-interactive Claude Code run needs.

    The harness is extended through Claude Code settings (MCP, hooks), not here.
    `session_id` must be a UUID: claude --session-id accepts nothing else.
    """

    prompt: str
    system_prompt: str
    model: str                      # alias or full id; a provider/ prefix is stripped
    thinking: str = "medium"        # mapped to CLAUDE_CODE_EFFORT_LEVEL
    session_id: str                 # UUID — claude --session-id creates, --resume continues
    session_dir: str                # holds only the .created marker, not the session itself
    raw_output_path: str            # NDJSON stream lands here
    tools: Optional[list[str]] = None
    cwd: str = "."                  # set from run.repo_root — the codebase root agents work in
    evidence_paths: list[str] = Field(default_factory=list)
    evidence_snapshot_max_bytes: int = Field(default=536870912, gt=0)


def _valid_cost(value):
    """A missing, Boolean, negative or non-finite value is not a reported cost."""
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


class UsageBreakdown(BaseModel):
    """Tokens and the dollars they cost, per component, summed over a call.

    Records normalized provider usage: `input` EXCLUDES cache reads, which bill at their own
    (cheaper) rate — add them to learn the size of the prompt that was sent.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Thinking tokens. NOT a fifth component: measured across every session on
    # disk, reasoning is always <= output and the four components above always
    # sum to totalTokens, so reasoning is the thinking SHARE of output, billed
    # at the output rate. Report it nested under output, never added to it.
    reasoning_tokens: int = 0
    total_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0
    total_cost: float = 0.0

    def add_turn(self, usage: dict, total_tokens: int) -> None:
        """Fold in one pi `message_end` usage object.

        `total_tokens` is passed in rather than re-derived: the caller already
        computes it pi's way (totalTokens, else the sum of the parts).
        """
        cost = usage.get("cost") or {}
        self.input_tokens += usage.get("input") or 0
        self.output_tokens += usage.get("output") or 0
        self.cache_read_tokens += usage.get("cacheRead") or 0
        self.cache_write_tokens += usage.get("cacheWrite") or 0
        self.reasoning_tokens += usage.get("reasoning") or 0
        self.total_tokens += total_tokens
        self.input_cost += cost.get("input") or 0.0
        self.output_cost += cost.get("output") or 0.0
        self.cache_read_cost += cost.get("cacheRead") or 0.0
        self.cache_write_cost += cost.get("cacheWrite") or 0.0
        self.total_cost += cost.get("total") or 0.0

    def merge(self, other: "UsageBreakdown") -> None:
        """Add another call's usage — a phase that retries spends more than once."""
        for field in type(self).model_fields:
            setattr(self, field, getattr(self, field) + getattr(other, field))


class PiResult(BaseModel):
    reported_error: bool | str = False
    terminal_success: bool = False
    text: str = ""
    returncode: int = 0
    session_id: str = ""
    tokens: int = 0
    cost: float = 0.0
    cost_known: bool = False
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    timed_out: bool = False
    # Context occupancy after the LAST turn — not a sum. `tokens` bills every
    # turn; this is how full the window is right now, which is what the
    # visualizer's context bar measures against `context_window`.
    context_tokens: int = 0
    context_window: int = 0         # 0 when the registry declares no ceiling
