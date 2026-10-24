# Owner model choice before a run

The controller asks the owner which model and reasoning effort each role uses
before a live guarded run (`/kh-start`, `/kh-simple-sdlc`, `/kh-project
run-continuous`, and natural-language "execute the project").
The owner answers in the conversation. Nothing is typed into a terminal, and
the controller never asks spontaneously once the cycle has started. Running
roles keep the choices resolved at the start of that run.

## When to ask

Ask before launching the run, once:

- for a single task, once for that task;
- for a project run (`run-continuous`, "execute the project"), once for
  the whole batch, since the answers apply to every task in it.

Do not ask when:

- the request already names the models and efforts (pass them as given);
- the owner said to use the current setup ("same as before");
- nobody can answer: a scheduled, unattended or headless session. Use the
  current setup and state that in the report;
- the run is a `--dry-run`, unless the owner asked to preview specific choices.

## How to ask

1. Run the read-only listing with the same `--config` and `--profile` the run
   will use: `["uv", "run", "adws/adw_models.py", "--choices"]`. It writes
   nothing and calls no provider. It returns:
   - every role this run needs, including `project_architect` when project
     context is missing;
   - each role's `current` choice (what the run uses without an answer);
   - up to four `options`, drawn only from this project's configuration.
2. Ask with AskUserQuestion, one question per role, at most four questions per
   call. When five roles are needed, ask `project_architect` in a second call.
   - Header: the role name.
   - Options: the listed `options` in order. Label each `MODEL · EFFORT` and put
     the runtime in the description. Mark the first one `(current)`, and
     recommend it unless the owner said otherwise.
   - The owner can type `RUNTIME:MODEL:EFFORT` using claude_code or
     delegate_codex. A typed `MODEL:EFFORT` keeps the
     current runtime. Say this in the question text.
   - When a role has no `current` choice and fewer than two options, ask for it
     in plain text in the same format.
3. For every role whose answer differs from `current`, add
   `--use ROLE=RUNTIME:MODEL:EFFORT` to the run's argv (repeatable). Unchanged
   roles need no flag.

## Rules

- Offer only what the listing returned or what the owner typed. Never propose,
  complete or substitute a model id yourself.
- The script validates every answer before any dispatch (role, runtime and
  effort; a model name is taken as declared and checked at preflight). If it
  refuses an answer, show the owner the exact message and ask again for that
  role only.
- Answers apply to this run only; the next run starts from the recorded setup
  again. To keep them as the default, run
  `/kh-setup-models --set ROLE=RUNTIME:MODEL:EFFORT ... --apply`, and only
  when the owner asks for it. `--use` cannot be combined with
  `--save-profile`.
- Report the effective model and effort per role from the run's own record
  (the start preview or the run's `routing.json`, field `run_choices`), not
  from the answers.

## Changes for a later run

Persistent `/kh-setup-models --apply` writes are refused while a writer is active.
Wait until it closes, then record a default only when the owner requests it.
For a different one-run choice, pass `--use` before starting the later run.
Do not queue a mid-run change or edit saved controller state. Runtime compatibility
checks may reject a choice but cannot raise permissions or silently select a
different model. See the task/tool compatibility section below for native work and static limits.

## Profiles, independent roles and bounded fallback

The controller alone dispatches workers, enforces permissions and accepts results.
Keep one controller and one writer for overlapping paths. Model choice cannot
expand write scope, weaken checks or remove required review.

Resolve `planner`, `plan_reviewer`, `builder` and `reviewer` separately, including
`project_architect` for context preparation and `documenter` when required.
Never silently reuse another role.
An independent reviewer uses a separate session with the actual delta and original
requirements; it may use the same model/provider as the builder. Provider diversity
is a preference, not proof of independence or correctness.

An explicit `--config PATH` takes precedence over `KH_CONFIG`, then
`adws/adw_kh_config/kh.config.yaml`. The `current` profile preserves configured
agent settings. Named profiles describe project choices, not built-in price or
quality tiers. Listing and dry-run report local configuration/readiness without
authenticating accounts, installing CLIs or invoking models.

Use `--profile NAME` for one run. `--save-profile` records a default in
`.kh/routing.json` only when the owner asks; a dry-run does not save it. Preserve
configured tool restrictions, and keep requested and effective reasoning settings
visible. An unsupported effort is a configuration error. Profile-required checks
are additive and cannot remove existing requirements.
For a `delegate_codex` choice, explicitly set `tools: null`; do not silently
discard inherited tool restrictions.

Only explicitly configured fallback candidates are eligible, before dispatch
when local preflight proves a candidate unusable. Record each substitution and
its reason. A dispatched failure does not authorize provider replacement. Do not
replay an uncertain dispatched operation on another provider; establish its
terminal state first. Native retries remain governed by the controller's limits.

## Task tool compatibility

Requirements are declared in project-owned role config before startup.
source_write checks source-write configuration; shell checks native shell
configuration. Legacy swift/database capability names also require native shell
configuration without proving executable or database access. capability_probes is
accepted for strict configuration compatibility without effect; it launches no host probe.

A file-only builder may edit Swift source while the controller executes
configured Swift quality checks. Native role execution needs explicitly selected
tools and existing provider permission.
Static compatibility cannot attest resource access. Failures do not grant tools
or replace the owner's model. See
[test-command permissions](test-command-permissions.md).
