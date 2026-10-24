---
description: "Initialize, continuously execute, pause, and inspect the controller-owned project plan."
argument-hint: "[--root PATH] init --plan PATH [--dry-run] | amend --task ID --definition PATH --reason TEXT [--dry-run] | split --task ID --plan PATH --reason TEXT [--dry-run] | run-continuous [OPTIONS] | pause-after-current | controller-status | status | next | report | block --task ID --reason TEXT | recover --task ID --reason TEXT"
disable-model-invocation: true
---

## Execution contract

Resolve the current project's root and follow its applicable instructions and
existing authorization. Treat every argument as data using a subprocess argv
array or individually safe quoting. Preserve Arabic, spaces and shell characters
literally. Never interpolate raw arguments, use eval, or invoke slash commands
recursively. Check prerequisites; do not silently install providers or change
models. Missing or unknown options are errors. Do not create an additional
controller or infer permission to push, deploy or publish externally.

## Action

Mapped script: `adws/adw_project.py`.
Base argv: ["uv", "run", "adws/adw_project.py"].

Supported subcommands:

- `init --plan PATH [--dry-run]`: validate and import the complete project plan.
  `--dry-run` does not create a ledger. Use the owner's agreed requirements;
  the installed `project-plan.example.json` is an example, not an approved plan.
- `status`: read tasks, milestones, blockers and weighted accepted completion.
- `amend --task ID --definition PATH --reason TEXT [--dry-run]`: replace a
  pending or blocked task definition from one JSON task object. Retain its ID
  and require an already owner-accepted intent matching the new request.
- `split --task ID --plan PATH --reason TEXT [--dry-run]`: read a JSON list of
  at least two replacement task objects. The first retains the original ID;
  later IDs are new. Every request needs its own matching accepted intent.
  Children retain the milestone and total weight, inherit prerequisites,
  constraints, non-goals and checks, and cannot expand the original path scope.
  Downstream tasks wait for terminal replacements. Both revision commands
  refuse any active task or a completed target, use the controller lock and one
  SQLite transaction, append the reason and previous state to history, and set
  revised tasks pending. Old attempts, receipts and saved candidates stay intact;
  normal resume validation decides whether kept work still applies. Dry-run
  validates without writing. Never generate accepted intents on the owner's behalf.
- `next`: read pending tasks whose dependencies are already completed.
- `run-continuous [--wait-seconds N] [--mode full|light] [--config PATH]
  [--profile NAME] [--use ROLE=RUNTIME:MODEL:EFFORT ...]`: execute the finite recorded plan one ready task at a time,
  until completion, pause or failure. After every controller-accepted task it
  emits `KH_TASK_COMPLETED` with accepted summary, progress and next task.
  Relay that event to the owner immediately. It stops only at project completion,
  an owner pause, a failed task, missing readiness, or interruption. It remains a
  foreground controller attached to this conversation, not a resident scheduler.
  Stale context is independently refreshed under the same lock and role choices;
  a rejected refresh or an unresolved owner decision stops execution.
- `pause-after-current`: cooperative safe pause for the active continuous run.
  The current task finishes through checks and review; no next task is dispatched.
- `controller-status`: read foreground continuous-controller state without
  starting work. Resume by starting `run-continuous` again; it uses ledger state
  and never re-runs an accepted task.
- `report`: regenerate Markdown and JSON documentation from the ledger.
- `block --task ID --reason TEXT`: record a blocker for a pending/blocked task.
- `recover --task ID --reason TEXT`: mark an interrupted active task blocked;
  refuses while a controller still holds the repository lock.

Optional `--root PATH` must precede the subcommand. Do not add a `complete`
operation or edit the database to mark work done. Execute a recorded task with
`adws/adw_start.py --task ID`; its controller owns the acceptance receipt.

Natural-language requests such as `execute the whole project` or `execute the project
to completion` use `run-continuous` after plan and ticket review. Execution is
sequential. `pause after the current task` maps to cooperative pause and
`continue the project` resumes from the ledger. Historical parallel journals and
execution decisions are rejected with `LEGACY_PARALLEL_STATE`; preserve their
files and reconcile using the prior release before starting new work.

Report actual command results. Completion percentage counts only weighted
accepted tasks; it is not elapsed time, effort spent, or an estimated finish date.
Reports are local project documentation. Do not send them to external services.

## Model choice for this run

Before `run-continuous` (including a natural-language request to
execute the project), ask the owner which model and effort each role
uses, following `.claude/skills/kh/references/model-choice.md` (read it
first). In short: run `["uv", "run", "adws/adw_models.py", "--choices"]`
(read-only), ask with AskUserQuestion one question per role (current choice
first, only the listed options, a typed RUNTIME:MODEL:EFFORT allowed), then add
`--use ROLE=RUNTIME:MODEL:EFFORT` for each changed role. Ask once for the whole batch,
before it starts, never between tasks. Skip asking when the request already names the models,
the owner said to keep the current setup, or no one can answer (then use the
current setup and say so). Answers apply to every task of this batch only.

## User arguments

$ARGUMENTS
