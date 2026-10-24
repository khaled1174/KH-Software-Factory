# Claude Code slash commands

This is the single catalog of 18 explicitly invoked command prompts and twelve
mapped scripts. The project installer copies these exact bytes to
.claude/skills/kh/references/commands.md; /kh-help reads that project copy.
Read START_HERE.md in the distribution for installation and usage.
User installation supplies commands only; every project still needs its runtime.
All paths in this catalog resolve from that target project root, including the
references below, even when this file is viewed in an installed reference directory.

| Command | Purpose | Script/action |
| --- | --- | --- |
| /kh-bootstrap | Prepare independently reviewed project navigation | adws/adw_bootstrap.py |
| /kh-plan-review TASK | Review/revise a plan without implementation | adws/adw_plan_review.py |
| /kh-project | Plan, progress, task revision and project execution | adws/adw_project.py |
| /kh-setup-models | Preview or persist owner role model/effort choices | adws/adw_setup_models.py |
| /kh-models | Inspect local profiles/prerequisites | adws/adw_models.py |
| /kh-start TASK | Full guarded plan/build/check/review | adws/adw_start.py |
| /kh-scope RUN_ID | Inspect the selected/latest guarded scope | adws/adw_scope.py |
| /kh-prompt TASK | One role task; read-only scout by default | adws/adw_prompt.py |
| /kh-scout TASK | Explore the project | adws/adw_scout.py |
| /kh-quality TASK | Run configured deterministic checks | adws/adw_quality.py |
| /kh-document TASK | Document changes against --base REF (default main) | adws/adw_document.py |
| /kh-simple-sdlc TASK | Alias for /kh-start --mode light | adws/adw_simple_sdlc.py |
| /kh-sessions | Latest ten recorded runs | Read-only trace query |
| /kh-phases RUN_ID | Recorded phases | Read-only trace query |
| /kh-tail RUN_ID | Latest 25 recorded events, one snapshot | Read-only trace query |
| /kh-procs RUN_ID | Recorded processes; no PID signalling | Read-only trace query |
| /kh-obs | Optional local trace UI | Existing just obs recipe |
| /kh-help | Read this catalog | No workflow execution |

## Task and guarded options

TASK is literal text or an intentionally supplied task-file path. Preserve it
as data, never shell syntax. Use -- before text beginning with a dash.
Task commands accept leading --config PATH and --adw-id ID where documented.
Configuration selection is explicit --config, then KH_CONFIG, then the project's
default config. A run ID preserves controller evidence; it is not implicit resume.

Guarded start accepts --task ID instead of positional TASK, --mode full|light,
--profile NAME, --scope-file PATH, --dry-run, --save-profile,
--refresh-context, --review-existing and repeatable --use ROLE=RUNTIME:MODEL:EFFORT.
Tracked --task refuses conflicting task text and cannot combine with --scope-file.
--review-existing reuses only the task's valid retained checked candidate and
plan; stale source/evidence refuses it. It cannot combine with --refresh-context.
--use is selected before the run and cannot combine with --save-profile.
--advisory requires --review-existing: it writes a review receipt without
promotion, task-state changes or edits to the saved candidate; exit 0 means
approved and 2 not approved. Repeatable --evidence PATH delivers an original
owner-named record unchanged, with its path and SHA-256, to every role.

/kh-simple-sdlc selects guarded light mode and the corresponding documented
guarded options. It preserves required checks and independent review.
The retired unguarded chains have no commands here. Effective allowances and
send defaults are documented only in
configuration (`.claude/skills/kh/references/config.md`).

/kh-bootstrap accepts --config, --profile, --dry-run and --refresh.
/kh-plan-review accepts --config, --profile, --scope-file, --dry-run and --adw-id.
A standalone plan approval cannot complete a task or authorize a hidden --use-plan
shortcut. Planning rules (`.claude/skills/kh/references/reviewed-planning.md`)
and intent rules (`.claude/skills/kh/references/intent.md`) define prerequisites.

## Model choices

/kh-models accepts --config, --choices and --doctor without authentication.
/kh-setup-models accepts repeated --set ROLE=RUNTIME:MODEL:EFFORT or --interactive;
--apply persists only when idle. Supported effort/tool declarations remain explicit.
Names are owner declarations, not a bundled catalog. Per-run --use overrides
do not queue a mid-run change or broaden permissions.
Model choice (`.claude/skills/kh/references/model-choice.md`) defines role
independence, profiles and explicit preflight fallback.

## Project subcommands

Global --root PATH precedes the subcommand.

| Subcommand | Required/optional options and result |
| --- | --- |
| init | --plan PATH; optional --dry-run; imports once |
| amend | --task ID --definition PATH --reason TEXT; optional --dry-run |
| split | --task ID --plan PATH --reason TEXT; optional --dry-run |
| status, next | Read committed progress or ready pending tasks |
| run-continuous | --mode full|light, --wait-seconds 0..60, --config, --profile, repeated --use |
| pause-after-current | Cooperatively stops continuous execution between tasks |
| controller-status | Reads continuous-controller state |
| report | Regenerates derived reports |
| block, recover | --task ID --reason TEXT; record blocked, never completed |

Project execution uses run-continuous. Historical parallel records are refused
with LEGACY_PARALLEL_STATE without changing their contents. The
project contract (`.claude/skills/kh/references/project-tracking.md`) defines
ledger schemas and recovery; no saved parallel approval is converted.

/kh-prompt accepts --agent NAME (default scout), --config and --adw-id.

## Monitoring and acceptance

The four trace commands accept --db PATH; sessions takes no run ID.
Trace commands use installed adws/kh_commands.py with read-only parameterized
SQLite queries; missing databases are not created. An omitted run ID selects
the latest recorded run explicitly. Recorded PIDs are not proof of liveness.
The optional UI needs the separately installed visualizer source, just and Bun.

No command creates automatic commits; defaults.auto_commit is accepted without
effect. No model completion, command example or shortcut grants push, merge,
deployment or broader permissions. Actual configured checks, independent review
and controller acceptance remain mandatory for guarded work.
/kh-kill and /kh-rosters are not shipped. General installation into other CLI
hosts is not implied by Claude Code command files.
