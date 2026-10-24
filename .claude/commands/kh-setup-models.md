---
description: "Record the owner's per-role model and effort choices at project start."
argument-hint: "[--config PATH] [--profile NAME] [--set ROLE=RUNTIME:MODEL:EFFORT ...] [--supported ROLE=effort,effort ...] [--interactive] [--note TEXT] [--apply]"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[guarded execution contract](../skills/kh/references/execution-contract.md#guarded-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_setup_models.py`.
Base argv: ["uv", "run", "adws/adw_setup_models.py"].
Supported options: --config PATH, --profile NAME, --set ROLE=RUNTIME:MODEL:EFFORT
(repeatable), --supported ROLE=effort,effort (repeatable),
--interactive,
--note TEXT, --apply.

The owner decides the model and the reasoning effort for each guarded-startup
role (project_architect, planner, plan_reviewer, builder, reviewer and documenter); this command records those explicit choices,
it never proposes a model catalog or invents model IDs. A model name is
recorded as declared — checked only as a non-blank string — so each recorded
name must identify a model actually available in the chosen runtime; unknown
roles, runtimes and efforts fail closed. Routable roles are
project_architect, planner, plan_reviewer, builder, reviewer and documenter;
scout keeps its config-level settings. Supported runtimes are claude_code and
delegate_codex only. An effort outside
the runtime's supported set is an error. Switching a role to delegate_codex
records an explicit tools: null and refuses when the agent carries extensions.
Antigravity, Gemini CLI and pi are retired. Existing .kh/models.json entries are rejected before
routing, including fallback entries and per-run overrides. Use an explicit
`--set ROLE=RUNTIME:MODEL:EFFORT` to replace a retired primary with a supported
runtime. Other roles, limits and fallback choices are preserved. Explicitly
remove or replace retired fallback entries in .kh/models.json before setup;
KH never converts or drops an owner's choices automatically.
See [runtime configuration](../skills/kh/references/config.md) for copyable settings.

Planner and plan reviewer are independently selected; the same model may be chosen
explicitly, but their phase contexts remain separate. Existing partial choices
remain valid configuration; missing roles prevent only workflows that need them.
Never fill missing owner choices silently.

Default behavior is a dry-run preview. `--apply` writes `.kh/models.json` and
`.kh/routing.json` atomically with rollback and selects the recorded profile.
It coordinates with the private repository writer lock `.kh/controller.lock`.
An active writer refuses changes to preserve the running task's owner baseline.
Wait until it ends, then record choices for a later run. This command never
starts a provider process, opens a provider session or makes network calls.
The preview's `effects` reports these counts as zero and names the configuration
paths and paths written. This does not guarantee approval by an external
native-permission classifier.

Recorded choices are configuration, not proof: CLI presence, authentication,
model access and live effort support stay unverified until guarded-startup
preflight. Show the preview JSON; after applying, suggest `/kh-start --dry-run`
before the first live run. Running roles keep their resolved model choices;
do not edit controller state to change them. Per-run `--use` choices are made
before starting a new run and do not persist project defaults.

## User arguments

$ARGUMENTS
