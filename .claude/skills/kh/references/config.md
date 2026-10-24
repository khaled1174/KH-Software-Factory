# Project configuration

Default: adws/adw_kh_config/kh.config.yaml. Preserve explicit --config and
KH_CONFIG selections; never change the owner's model assignment implicitly.

## Upgrades keep project configuration and prompts

`install.py --upgrade` never overwrites an existing project
`adws/adw_kh_config/kh.config.yaml`. The install report lists it in
`kept_project_files` and saves the package's version as
`.kh-backups/kh.config.package-<hash>.yaml` (same hash, same name; a newer
template gets its own name). `--reset-config --project <path>` replaces it with
the template through the normal backed-up upgrade path.

Existing differing role prompts under `adws/adw_data/prompt_engineering/` are
also kept. A literal configured `system` or `user` path inside the project is
kept whenever that path is an installer destination. The report lists preserved
files in `kept_project_files`. Package prompt copies are saved under
`.kh-backups/package-files/<destination-parent>/<name>.package-<hash>.md` for
comparison. `--reset-config` never resets prompts. Nonliteral YAML references
are not resolved by the installer's dependency-free path reader.

Runtime code and framework instructions still update. Compare the kept role
prompts with the new package and merge required prose deliberately; the installer
does not silently merge or discard owner text. Plan reuse remains bound to the
effective prompt and role digests, so a changed prompt or role choice requires a
fresh plan rather than reusing an approval for the old instructions.

## Role timeouts and command limits

- A role stopped by its time limit (`timeout_seconds`, default 1800 s for every role)
  fails with a headline that leads with the cause: `ROLE TIMEOUT`, exit 124, the
  elapsed time and the command still running when it was cut. It is never
  retried as a provider outage.
- The builder is told a per-command limit by the controller: a quarter of the
  role's time, between 30 s and 600 s (the controller's own `test` gate limit).
  Other transports receive this as an instruction rather than an externally
  enforced cap for every agent command; capture helpers enforce their own cap.
- During a role the controller retains observed descendant birth identities;
  after a timeout it stops those still matching, live descendants, and exact
  inherited `KH_ROLE_TAG` matches on Linux and macOS. This covers observed native
  children after reparenting even when macOS hides their environment. A child
  that detaches before discovery and has no readable tag cannot be identified
  this way. Identity validation and signaling remain separate syscalls on macOS.
- Claude Code's context window is read from the CLI's own result
  (`modelUsage.<model>.contextWindow`). `KH_CC_CONTEXT_WINDOW` pins it
  explicitly; 200K applies only when neither is available.
- The last plan revision is checked mechanically while the planner is still in
  its session: a coverage/step mismatch, an unknown step or requirement, or a
  missing required check is sent back once with the validator's exact message
  before the run is stopped. A wrong `changed_files` claim from the builder is
  corrected up to two extra times from the controller's measured file set
  without using the phase's retries.

## Defaults

- coding_agent: claude_code or delegate_codex
  (see the transport sections below).
- auto_commit: retained for configuration compatibility; it has no effect.
- quality_checks: map test/lint/typecheck/build to real argv arrays.
- required_quality_checks: nonempty subset run by the quality workflow.
- test_environment_overrides: default `{}`. Maps only `DATABASE_URL` or
  `TEST_DATABASE_URL` to nonempty lists of exact literal values. Adds two exact
  process-local environment forms to test-class captures only. Values cannot
  contain `$`, backticks, control characters or newlines.
- required_test_commands: default `[]`. Exact single-line native shell commands
  the owner requires the builder to execute. These are separate from host
  quality checks and do not grant tools or command permissions. An approved
  plan may add requirements through `PlanOutput.required_test_commands`;
  approval does not add grants.
- snapshot_max_bytes: captured-content budget, default 536870912. Oversized
  projects stop before dispatch; configure a larger budget deliberately.
- generated_paths: disposable untracked output directories, default **/.next/,
  **/dist/, **/coverage/, **/__pycache__/, **/.pytest_cache/, **/.mypy_cache/,
  **/.ruff_cache/, **/.turbo/, **/.expo/, and the toolchain build folders
  **/.build/ and **/.swiftpm/ (SwiftPM), **/.gradle/ and **/.dart_tool/. These
  are not promoted, and a check that fills them is not a source change. A
  check that writes build output elsewhere (plain build/ or target/, left out
  because projects keep source there) is refused as a source change: add that
  folder here. Configure [] if such folders contain source in the project.
  Tracked directories stay source. A project list replaces the defaults, so
  copy the defaults you still want.
- dependency_paths: input directories excluded from retained source snapshots,
  default **/node_modules/. Copied independently; streamed content fingerprints
  detect check mutations and owner dependency changes. Lists replace defaults.
- protected_files: controller-owned path prefixes and globs.
- data_dir: normally adws/adw_data.

Example only; choose commands that actually exist in the project:

```yaml
defaults:
  auto_commit: false
  quality_checks:
    test: [npm, test, --, --run]
    lint: [npm, run, lint]
    typecheck: [npm, run, typecheck]
    build: [npm, run, build]
  required_quality_checks: [test, lint, typecheck, build]
```

Unknown configuration keys are errors, including unsupported budget fields.
Cost never stops work: there is no spending cap. Every tracked task reports what
it spent (dollars where the CLI reports a price, tokens always) in its receipt
(`cost`), in its task report ("## Cost") and, for a failed task, at the end of
its blocker. Partial model setup
preserves the selected owner's profile and unrelated role/capability settings.

`execution.recovery_rounds` defaults to 2 in full mode. It permits a supervised
Claude/Codex builder cutoff to continue from safely retained staging, a
repair window with candidate or finding progress to continue, or a concrete
missing plan path to receive a new independently approved plan. Light mode and
an explicit zero repair allowance retain their original limits; setting
`recovery_rounds: 0` disables recovery. Failed attempts and prices stay in the
trace. A cutoff makes the price explicitly partial, including priced turns
already reported before interruption.

The original deadline, configured send ceiling, role permissions, accepted
task scope, checks and owner-decision requirements remain in force. Counters
never reset. Without an explicit send ceiling, full-mode capacity reserves the
declared recovery windows. Default full/light ceilings are 34/14 sends without
closing documentation and 43/16 with it; `--dry-run` reports the effective
configuration under `execution`. A plan amendment
preserves prior checks, criteria and step IDs and adds only reviewed concrete
files inside the original owner's explicit scope. It records immutable parent
approvals and versioned plan packets before changing the effective boundary.
Unknown transport failures, forbidden writes and unresolved owner decisions
stop; an unchanged candidate with unchanged finding resolutions cannot extend
repair windows indefinitely. These are technical stops, independent of spend.

## Owner review (reported after completion)

Guarded `/kh-start` (full and light, including `/kh-project` runs) detects
when the verified delta touches a category the owner cares about. By default it
never pauses the run: the owner hears about it after the task closes. It is on
by default:

```yaml
execution:
  owner_review:
    enabled: true
    mode: report                  # report (default) | hold
    max_changed_lines: 400        # `size` category; 0 disables it
    # size_exclude: [...]         # lockfiles and generated output by default
    # categories:                 # replaces the defaults below when set
    #   money:  {paths: ["**/billing/**", "**/payments/**", "**/stripe/**", "**/checkout/**"],
    #            keywords: ["stripe.", "payment_intent", "price_id", "invoice.create"]}
    #   auth:   {paths: ["**/auth/**", "**/authz/**", "**/permissions/**"],
    #            keywords: ["jwt.sign", "bcrypt", "password_hash", "service_role"]}
    #   schema: {keywords: ["drop table", "drop column", "truncate table", "rename column", "drop schema"]}
```

Matching is deterministic and reads only controller measurements: changed paths
(case-insensitive; `**/` spans any directories, `*` stops at `/`, a pattern
ending in `/` covers everything under it) and the *added* lines of the exact
snapshot the checks and reviewers judged (keywords, case-insensitive). Text that
is not UTF-8 is read as latin-1, so an encoding cannot hide a keyword or a large
change; NUL-bearing binaries are matched by path only. `size` counts added plus
removed lines outside `size_exclude`. Patterns are root-relative POSIX globs; a
leading `./`, `.` or `..` segments and backslashes are configuration errors.

**`mode: report` (default).** The checks run first. Each hit then becomes an
exact controller requirement ("Owner attention (category): …") for the
independent reviewer. The requirement is met only when the task needs that
change and it is safe to land unattended. An unmet requirement, or no ruling
on it, blocks approval like any other blocker, and the normal repair loop runs.
Once approved, the task promotes and closes itself. Every hit is recorded:
under `owner_review.flagged` in the receipt, under "For the owner (after
completion)" in the task report (`.kh/project/tasks/<ID>.md`), and on the
console when the run ends. Run-next and continuous batches never stop for it.

**`mode: hold` (opt-in).** A hit fails the task with `OWNER_REVIEW_REQUIRED`
before promotion. The message names each category and its first piece of
evidence, nothing is promoted, and the controller diff stays in the run's
published evidence. To let a held task promote, list the category under
`Approved sensitive changes` in the task's intent, re-accept it, and run the
task again. Use this mode only when you want to sign off before anything
lands: it stops the batch at every flagged task.

In both modes an approval naming an unknown category fails before any model
dispatch, and approved categories are not flagged. There is no command-line
override. Keyword lists catch the obvious forms, not every spelling. Jev risk
hints remain advisory and do not feed this decision.

## Floor guard before review

Agents that hit a red check tend to take the cheapest road to green. After the
checks pass, the controller scans the verified delta (its own snapshots, not
the builder's report) for moves that lower the bar, adapted from the
agent-skills floor guard:

| Rule | Detected in | Examples |
|---|---|---|
| `test-made-easier` | added lines of test files | `@pytest.mark.skip`, `xfail`, `.skip(`, `.only(`, `self.skipTest(` |
| `test-deleted` | a removed test file | |
| `assertions-removed` | a test file with fewer assertion lines than before | reordering is not a finding |
| `silenced-checker` | added lines of any file | `# noqa`, `# type: ignore`, `# nosec`, `eslint-disable`, `@ts-ignore`, `istanbul ignore`, `gitleaks:allow` |
| `unfinished-work` | added lines of source files | `raise NotImplementedError`, `except …: pass`, empty `catch {}`, `TODO`, `FIXME` |
| `threshold-loosened` / `threshold-removed` | check configuration (`pyproject.toml`, `setup.cfg`, `.coveragerc`, `package.json`, jest/vitest/eslint config, `Makefile`, `.github/workflows/*`) | `fail_under` or coverage thresholds lowered, `max-warnings` raised |

Each finding becomes an exact controller requirement for the reviewer — "met"
only when the task itself requires it and the reviewer says why — so the
verdict gate refuses approval while one is unmet. Findings are recorded in
`floor_guard_<attempt>.json` in the run evidence and name rule, path, line and
marker only, never the line's text. Tightening is silent. Disable with:

```yaml
execution:
  floor_guard:
    enabled: false
```

The patterns are regular expressions: they catch the common forms, not a
determined attempt to hide a change.

The retired `execution.jev_scout.enabled` Boolean setting remains accepted
without effect for one compatibility release. The controller does not call Jev
or send task text or paths to that service. Normal project context, checks and
independent review remain unchanged.

Checks must leave source and dependency inputs unchanged. Formatters and source
code generation belong in implementation, followed by current checks. Disposable
build output may be created in staging; it is not copied into the owner checkout.

Missing commands never pass. Test workflows check their configuration before
worker dispatch. No project test command is guessed or installed.

## Agents

Fields: name, coding_agent, model, thinking, prompt_engineering, tools, writes,
timeout_seconds (default 1800), required_capabilities, capability_probes, and
optional delegate_relay_path/delegate_sandbox.
writes: [] prevents source promotion; null permits nonprotected source paths;
a list restricts source paths. Each worker also owns its supplied report directory.
Protected paths cannot be unlocked by broad worker allowlists. Nested .env files,
control definitions and prior evidence are protected.

tools: null uses provider-configured capabilities where the runtime supports
that contract. An empty list disables built-in Claude tools.
These flags are not an OS sandbox; copy isolation is not OS confinement. Provider-specific MCP
and extension behavior is described in the transport sections below.

### Required task capabilities

`required_capabilities` declares `source_write`, `shell` and the legacy
`swift`/`database` names. Checks enforce configured tools, source-write boundaries,
sandboxes and exact native-command grants before dispatch. Swift/database names
require native shell configuration; executable availability and resource access
remain unverified until actual tool receipts. Source-only Swift work can retain
file tools while the controller runs configured quality commands.

Role `capability_probes` is retired and accepted without effect for one
compatibility release. Existing entries retain strict schema validation but
launch no host commands. This does not change the separate owner restrictions
in `execution.infrastructure.capability_checks`. A failed compatibility check
never elevates permissions or substitutes the owner's model.

The retired `review.panel` list is accepted without effect for one compatibility
release. Each review round has one independent combined reviewer; legacy lens
names neither add sessions nor change execution limits or light eligibility.

Owner `defaults.required_test_commands` are checked before planning/boot.
Approved-plan test commands are checked before the builder call. All
transports check known tool and infrastructure restrictions, including commands
declared as quality checks. Native permission remains unverified until an actual
tool receipt establishes it. A known mismatch prevents the builder model call;
it does not erase earlier planning usage. File-only builders can retain host
quality checks with an empty native-test requirement list. See
[test-command permissions](test-command-permissions.md) for setup and diagnosis.

### Claude Code permissions in the isolated copy

Every role runs in a fresh copy of the project that Claude Code has never
trusted. In `-p` mode Claude Code then ignores the project's
`.claude/settings.json` ("this workspace has not been trusted"). Under
`--permission-mode default` it also refuses the evidence file each role must
write, so every Claude role failed its first gate. Claude roles now run with:

- `--permission-mode acceptEdits`: edits are accepted inside the working
  directory (the copy) only. An edit anywhere else still needs a permission no
  one can give, and the controller's scope check decides what is promoted.
- `--settings <generated file>`: carries your own `permissions.allow`, `deny`
  and `ask` rules from `.claude/settings.json` and `.claude/settings.local.json`.
  `Bash(swift:*)` and similar rules work as they do in your own sessions. Edit
  grants (Edit, Write, MultiEdit, NotebookEdit) are dropped: inside the copy
  they are redundant, and outside it they would bypass isolation.
  Settings-relative `/path` rules are anchored to the copy. The file is written
  outside the copy, where an agent cannot edit its own grants, and removed
  after the send. A copy of what each send was granted is kept as
  `claude-settings.json` beside its `raw_output.jsonl`.

Claude Code still prints "Ignoring N permissions.allow entries from
.claude/settings.json: this workspace has not been trusted" for the copy's own
file. That warning is expected and harmless: the same rules arrive through
`--settings`. No `bypassPermissions` is ever used. Other project settings (hooks, MCP
servers) are not carried.

### Claude Code roles do not inherit your global Claude Code setup

Without flags, every `claude -p` a role runs loads everything installed for
your account: user MCP servers, claude.ai connectors (Gmail, Notion, Supabase…),
plugins, skills and slash commands, user agents, `~/.claude/CLAUDE.md` and user
hooks. On a busy machine that came to 324 tools and 291 skills sent with every
request. A reviewer spent 318K tokens before reading a file and stopped with
`SCOPE_TOO_LARGE_FOR_REVIEW`, and every role could call tools it was never
given. Claude roles now also run with:

- `--strict-mcp-config`: no MCP server from any settings file.
- `--setting-sources project,local`: your user settings (`~/.claude/settings.json`)
  are not loaded, and with them the plugins enabled there, the user CLAUDE.md and
  user agents.
- `--disable-slash-commands`: no skill or slash command, project ones included.
- `--no-chrome`: no Claude in Chrome tools.
- `disableClaudeAiConnectors: true` in the generated settings file and
  `ENABLE_CLAUDEAI_MCP_SERVERS=false` in the role's environment: no claude.ai
  connectors.

Sign-in is unaffected: a subscription sign-in (OAuth, macOS keychain) and
`ANTHROPIC_API_KEY` are not settings. If your sign-in or model setup lives in
`~/.claude/settings.json` (`apiKeyHelper`, an `env` block), roles lose it; set
`KH_CC_INHERIT_USER_SETUP=1` in `.env` to go back to inheriting the whole
setup. The flags need Claude Code 2.x; an older CLI rejects them at the first
send. Codex uses its separate relay contract.

## Model choice per run

Before each live run or batch, the controller asks the owner in the
conversation which model and effort each role uses
([model-choice.md](model-choice.md)). The answers reach the scripts as
`--use ROLE=RUNTIME:MODEL:EFFORT` (repeatable) on `adw_start.py`,
`adw_simple_sdlc.py` and `adw_project.py run-continuous`. They replace
the selected profile's choice for those roles in that run only. They are
validated like `/kh-setup-models` before any dispatch, recorded in the run's
`routing.json` under `run_choices`, and never written to `.kh/`. A batch
validates them before it takes the repository lock and applies them to every
task. `adw_models.py --choices` is the read-only menu the controller asks from:
the current choice per needed role first, then up to three other choices
already configured in the project. `--use` with `--save-profile` is refused;
keep choices with `/kh-setup-models --apply`.

## Codex transport

Set coding_agent: delegate_codex, an explicit model, tools: null, and
harness_engineering: []. The project must be a git repository: Codex refuses to
run outside one ("Not inside a trusted directory"), and a project that is not a
repository is copied as plain files. Validation reports this before dispatch;
`git init` once is enough. workspace-write is limited natively to the isolated
workspace; the controller checks narrower source promotion scope. read-only is
suitable only when no evidence-file writes are required. Unsupported tool or
extension restrictions fail before dispatch.

The relay receives explicit task context, baseline, cwd, model, required evidence
and output schema. Completion is not acceptance. See [delegation](delegation.md)
for pinned sources, supported POSIX host and live-pilot limits.

Network: the pinned relay runs Codex workspace-write with network access off,
so a Codex builder cannot download packages (SwiftPM, npm, pip) itself. The
controller's required checks run outside that sandbox and are what decide
acceptance; a builder that cannot build fixes from their reports instead of
trying the build itself. To let it build offline, resolve dependencies once in
your own tree (`swift package resolve`, `npm install` ...): declared generated
and dependency folders (**/.build/, **/node_modules/ ...) are copied into
every isolated copy. Whether a toolchain then builds fully offline inside the
Codex sandbox depends on that toolchain's own caches outside the project.

## Provider outages

A send that fails because the provider is overloaded, at capacity, returning
5xx, dropped the connection, or a request was aborted before any reply is
repeated after execution.transient_retry_delay_seconds, then twice that, then four times
(default 60 s, 120 s, 240 s), up to execution.transient_retries times per send
(default 3; 0 turns it off). KH retains its logical session identity: Claude
resumes its native conversation; Codex receives the approved context again
without an inherited native conversation. Only error output is read: the transport error,
stderr and error events, never model text. A usage limit, quota, refused key,
bad request, a gate or budget stop is not retried. A controller-imposed timeout
is never transient-retried, even when captured output also mentions an outage.
Retries spend no model-send or correction budget; each is checked
against the run deadline first, counted as transient_retries in the execution
budget, and traced as a transient_retry event. When the outage outlasts the
retries the run stops with "provider outage persisted after N retries".

## Re-running a task

A task's run that stops before acceptance does not lose its work, and a re-run
continues it.

- Plan: a new run whose request, scope, project context (including the
  accepted intent), planner and plan-reviewer settings, execution mode and
  builder-writable source are unchanged reuses the plan an earlier run had
  approved. Control and config files (adws/, .claude/, .kh/, intents are
  covered by the context) do not count, so raising a role timeout or changing a
  check command keeps the plan. The earlier plan and review are checked against
  their recorded digests and validated again; the new approval names
  reused_from and reused_plan_digest. execution.reuse_approved_plan: false
  always plans again.
- Work: when the run stops (role timeout, provider failure, a stopped
  controller, failed checks, a rejected review), the builder's permitted
  changes are taken from its phase copy into the task stage under the same
  checks as a promotion, and the stage's candidate changes are kept in
  .kh/work/<task>/current/ with a manifest of sha256 digests, the builder's
  own step note (progress.json, which the builder is asked to keep up to date)
  and plan.md marking the steps it reported done. The next run of the task, on
  the same builder-writable source and plan content, applies them to its fresh
  stage after plan approval and tells the builder to continue, including what
  stopped the last attempt. Applied work goes through the normal checks, scope
  review and independent review. Kept work that no longer matches (the source
  changed, a different plan, a digest mismatch, a path the builder may not
  write, unreadable data) is moved to stale/ with why-not-resumed.txt and the
  run starts clean. Accepted work clears current/. .kh/work is never copied
  to agents, never part of a source fingerprint and is git-ignored.
  execution.resume_failed_builds: false turns it off.

## Startup profiles and task scope (3.0)

`routing.default_profile` selects a named profile in `routing.profiles`.
The bundled `current` profile inherits planner/builder/reviewer assignments.
A profile's `roles` maps those names to ordered explicit choices containing
coding_agent, model, thinking and supported_thinking. The declared effort list
is local configuration, not an authenticated model capability catalog. A switch
to delegate_codex requires an explicit tools: null choice. Validation is
syntactic and fail-closed: an unknown role, runtime or effort is an error,
while a model name is recorded as declared — checked only as a non-blank
string — so each recorded name must identify a model actually available in
that runtime. Profile choices cannot override source writes, install
providers, remove required checks or reduce review sessions: a profile
records role model, effort and supported tools under the controller's rules.
Fallback is considered only when an earlier configured CLI is absent before any
dispatch. Runtime failure never starts a different provider automatically.

### Model choices before a run

Resolve per-role choices before startup. Per-run `--use` overrides apply only to
that run. Persistent `/kh-setup-models --apply` writes require an idle repository;
an active writer refuses them. There is no mid-run queue or model-switch state.
Scope, native permissions, checks, deadlines, accumulated costs and attempt
counters remain in force. See [model choice](model-choice.md) for
setup examples and task/tool compatibility limits.

Run `uv run adws/adw_models.py` to inspect profiles. Use
`uv run adws/adw_start.py TASK --profile NAME --dry-run` to inspect selection
without writes. `--save-profile` explicitly saves the chosen name for this
project in .kh/routing.json; dry-run never saves. An ordinary --profile is
one-run only. The startup command always requires planner/build/check/review;
older workflow entry points retain their individual scope.

An optional --scope-file JSON supplies original_request, deliverables, non_goals,
constraints, allowed_paths, required_checks and review_required. Missing scope
input defaults to the exact request with unrelated work excluded; no speculative
file boundary is invented. Plan criteria are additive to original constraints.
When the scope file omits allowed_paths (or no scope file is given), the plan's
allowed_paths become the builder's frozen boundary; a supplied owner list can
only be narrowed by the plan (pattern containment must be provable, and real
files are checked against both the owner's and the plan's boundary). The controller writes controller_diff_N.patch (the
measured task delta) into task evidence before each review and turns changed
test/check files into an explicit review requirement. The accepted plan is
re-sent on every repair attempt.
An existing run cannot silently change its request, scope or routing.

See the installed [model-choice reference](model-choice.md) and kh-scope-guard skill for
complete examples. Planner/reviewer artifacts go in their phase evidence
folders, while source remains read-only for these roles in guarded startup.

Prepare dependency installation or updates explicitly before guarded execution.
A worker cannot change its copied dependency inputs and authorize source that
would use different dependencies in the owner checkout.

## Command evidence

The controller records every shell command a coding agent runs, from the
agent's own event stream (Claude Code and Codex), in
`adws/adw_data/sessions/<run>/commands/<phase>/ledger.jsonl`: exact command
text, start and end time, the runtime's outcome and the complete output the
agent received (`streams/`, one file per invocation, SHA-256 recorded, empty
streams included). A start record is written before the result exists, so an
interrupted call leaves an `interrupted` record. Each line names the SHA-256 of
the previous line.

The command is analysed rather than trusted. A status hidden by a pipeline
without `set -o pipefail`, by `; echo $?`, `|| true` or background execution is
recorded as `unknown`; output cut by `tail`, `grep` or a redirection is recorded
as an `excerpt`. Each command is classified by `execution.command_policy.classes`
(regex over the command text, wrappers such as `env`, `timeout`, `xargs` and
`bash -lc` removed). Strict classes (default: test, format, lint, probe) need an
authentic exit status and complete output; `exploratory_compile` needs only the
command and timing record. A builder that ends with an incomplete strict
record is told, in the same session, which invocation and why; a later
complete re-run of the same command supersedes it, and the original stays
recorded as unknown.

After each phase the records are copied into the task handoff under
`controller_inputs/commands/<phase>/` with `coverage.json`, and every later
role's copy is verified byte-for-byte before its model is invoked
(`EVIDENCE_DELIVERY_ERROR` otherwise). The approved plan, approval, review and
provenance (digests, algorithm, coverage map, reuse explanation) are delivered
under `controller_inputs/plan/`.

### Owner decisions

An owner exception for specific past invocations is a record in
`.kh/dispositions.json` (no agent workspace contains it):

```json
{"schema_version": 1,
 "decisions": [{"id": "T3-X1", "task": "T1", "kind": "historical_exception",
                "authority": "Example owner decision",
                "recorded_at": "2000-01-01T15:00:00Z",
                "invocations": ["example-run#1"], "classes": ["format"],
                "substitution": "the controller format check of the final candidate",
                "does_not_establish": "the original exit status"}]}
```

It matches only the exact invocation IDs and classes it lists, and only
invocations that started before `recorded_at`. The effective policy (classes and
decisions) has one digest, delivered as `controller_inputs/policy.json`, rendered
into every prompt and written to the receipt. A report that cites a decision
that has no record, or applies one to an invocation it does not name, fails its
gate.

## Infrastructure boundary

File scope sees files. `execution.infrastructure` covers what it cannot:

- `worker_env` / `withhold_env`: the agents' environment (not the controller's
  quality checks). Give agents a non-superuser database URL and remove admin
  credentials or `DOCKER_HOST`. This is the layer that makes a mutation
  impossible: a non-superuser role cannot run `ALTER SYSTEM` by any path.
- `capability_checks`: argv probes run by the controller with the agents'
  environment before planning; the task refuses to start
  (`INFRA_CAPABILITY_NOT_RESTRICTED`) unless stdout equals `expect_stdout`.
- `deny_executables` (empty by default, since many test suites run
  `docker compose up`): replaced on the agents' PATH by a refusing stub and
  denied to Claude Code roles by permission rules. Absolute paths bypass PATH,
  so this is prevention, not proof.
- Detection over every recorded command: container tools (`docker`, `podman`,
  `nerdctl`, any path) with a subcommand in `forbidden_container_actions`
  (default restart, stop, kill, rm, down, pause, update, prune), `systemctl`/
  `launchctl`/`service` stop/restart, `pg_ctl`, `brew services`, judged per
  command stage (so `cat docker/start.sh` or `docker run --rm` are not
  matches), plus the `forbidden_commands` regexes (default: `ALTER SYSTEM
  SET/RESET`, `pg_reload_conf()`). An executed match stops the phase with
  `INFRA_BOUNDARY_VIOLATION`; a call refused as a whole is recorded as refused.
  Neither claims anything about the server's state.
- `resource_prefix`: agents get `KH_RESOURCE_PREFIX=<prefix><run>_`, and a
  `DROP SCHEMA`/`DROP DATABASE` of anything else is forbidden.

An explicitly referenced `${NAME}` must be present and nonempty in the worker
environment. Missing values stop with `INFRA_WORKER_ENV_NOT_CONFIGURED`; they
never become an empty database argument that could fall back to local defaults.
Worker overrides are validated before planning, even when no probes are configured.

For an existing restricted PostgreSQL credential, this configuration removes
admin variables, assigns the worker URL and verifies the role before dispatch:

```yaml
defaults:
  env_allowlist: [ANTHROPIC_API_KEY, KH_WORKER_DATABASE_URL]
execution:
  infrastructure:
    enabled: true
    withhold_env: [ADMIN_DATABASE_URL, DOCKER_HOST]
    worker_env:
      DATABASE_URL: "${KH_WORKER_DATABASE_URL}"
      TEST_DATABASE_URL: "${KH_WORKER_DATABASE_URL}"
    capability_checks:
      - name: worker-not-superuser
        argv: [psql, "${DATABASE_URL}", -tAc, "select rolsuper from pg_roles where rolname = current_user"]
        expect_stdout: "f"
```

Use an owner-supplied limited credential, not an admin URL renamed as a worker.
This example neither provisions a database nor changes server settings. A role
without database work need not configure it. Removing `DOCKER_HOST` alone does
not remove access to a local Docker socket; restrict the worker's actual access
when that capability is not intended. Detection is not a rollback mechanism.

## Deadline fit and review-only continuation

`.kh/timing-history.json` keeps observed planning, build, check and review
durations, separately for full and light runs. `execution.timing_preflight: warn` (default) reports in the start
preview whether `deadline_seconds` fits planning plus every permitted
build/check/review cycle; `refuse` does not start when it does not; KH never
raises the deadline or lowers repairs itself. During a run, judged only from durations this run has
itself measured, no build cycle starts that cannot also be checked and reviewed
in the remaining time
(`DEADLINE_INSUFFICIENT`), and no review starts that cannot finish
(`CANDIDATE_CHECKED_UNREVIEWED`). The checked candidate is kept with its check
results; `/kh-start --review-existing` (or `--task ID --review-existing`) then
reviews it without a builder round, reusing the approved plan or refusing, and
promotes only on a real approval.

## Closing documentation

`execution.documentation.enabled` enables scoped documentation proposals in
`/kh-start` and the guarded light workflow. New generated configs enable it.
Omitting the key in an existing config retains compatibility and produces an
explicit `not_configured` documentation receipt.

Select the documenter with the existing setup command, using your actual runtime,
model and supported effort: `/kh-setup-models --set documenter=RUNTIME:MODEL:EFFORT
--apply`. This changes only that role. With closing docs enabled, a recorded
choice or explicit per-run choice is required before dispatch; starter model
names never substitute for it. No source-write grant is added to the role.

The plan's documentation targets must fit the original owner scope and the
builder's existing permissions. Code and docs are checked and reviewed together.
The default send allowance reserves a proposal for each permitted build window;
an explicit owner ceiling and the original deadline still apply. Cost is only
reported. See [project context](project-context.md) for publication and resume boundaries.
