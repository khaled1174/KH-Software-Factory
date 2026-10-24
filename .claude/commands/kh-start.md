---
description: "Start a task with configured routing, bounded scope, checks and independent review."
argument-hint: "[--config PATH] [--profile NAME] [--use ROLE=RUNTIME:MODEL:EFFORT ...] [--scope-file PATH] [--dry-run] [--mode full|light] [--save-profile] [--adw-id ID] [--task TASK_ID] [--refresh-context] [--review-existing] [--advisory] [--evidence PATH ...] [TASK]"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[guarded execution contract](../skills/kh/references/execution-contract.md#guarded-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_start.py`.
Base argv: ["uv", "run", "adws/adw_start.py"].
Supported options: --config PATH, --profile NAME, --use ROLE=RUNTIME:MODEL:EFFORT
(repeatable), --scope-file PATH, --dry-run, --mode full|light, --save-profile, --adw-id ID,
--task TASK_ID, --refresh-context, --review-existing, --advisory, --evidence PATH (repeatable).

Full-mode recovery uses execution.recovery_rounds (default 2), the original
deadline and cumulative counters. Supervised native builder cutoffs can continue
safe partial staging; progressing repair windows can extend. A rejected review's
concrete repair_paths require a full revised plan and new independent approval
inside the owner's explicit task boundary. Costs are reported only. Review-only
work never enters recovery planning or builder dispatch; unresolved owner or
infrastructure blockers stop through the existing decision path.

--refresh-context independently reviews and republishes stale project navigation
before planning, using the same repository lock and recorded role choices.
It preserves owner documents and stops on unresolved owner decisions or a rejected
context review. It cannot accompany --review-existing, which binds the candidate's
original context. --dry-run previews this preparation without dispatch or writes.

--review-existing reviews the task's kept, already checked candidate (for
example one whose review was cut off by the deadline) without a builder round.
It reuses the approved plan or refuses, re-runs only checks whose recorded
evidence no longer matches the candidate bytes, and promotes only on a real
independent approval through the normal receipt. A rejection keeps the
candidate and its findings for the next normal run. Use it only when the owner
asks for a review of the existing candidate.

--advisory (only with --review-existing) reviews without consequences: nothing
is promoted, the task state and the saved candidate stay unchanged, and a review
receipt is written to .kh/work/<task>/reviews/<run>.json. Exit 0 = approved,
2 = not approved. --evidence PATH delivers an original record the owner names
(for example an older harness session log) unchanged, with its source path and
SHA-256, to every role of the run.
Both review-only modes require ready context; they refuse missing or stale
context without bootstrapping or changing navigation documents.

## Model choice for this run

Before launching a live run, ask the owner which model and effort each role
uses, following `.claude/skills/kh/references/model-choice.md` (read it
first). In short: run `["uv", "run", "adws/adw_models.py", "--choices"]`
(read-only), ask with AskUserQuestion one question per role (current choice
first, only the listed options, a typed RUNTIME:MODEL:EFFORT allowed), then add
`--use ROLE=RUNTIME:MODEL:EFFORT` for each changed role. Ask once, before the
run, never during it. Skip asking when the request already names the models,
the owner said to keep the current setup, or no one can answer (then use the
current setup and say so). Answers apply to this run only.


Read the installed [model-choice reference](../skills/kh/references/model-choice.md)
and kh-scope-guard skill to prepare the request. Reuse the selected project defaults unless the user chooses a profile.
--profile alone applies to this run. --save-profile changes the project default
only when the user explicitly wants it saved. Do not invent model IDs or change
provider authorization. The default profile current retains configured models.

For --task TASK_ID, use the recorded project task request and scope; no positional
task is required. Do not combine --task with --scope-file or rewrite the plan.
The ledger enforces dependencies and records accepted results and blockers.

Preserve an ordinary task as ONE positional argument, with -- immediately before it
when constructing argv. A file is task input only when intentionally supplied
by the user. Preserve its contents as request data. Pass a supplied scope file
without silently rewriting its constraints. When the conversation defines scope
but no file, a scope JSON may be prepared from those explicit requirements;
record its path and use the documented schema in kh-scope-guard. Avoid adding
unrequested work. If no task is unambiguously supplied, ask for the task.

Guarded execution requires a matching accepted `intents/*.md` record for the
task (see `.claude/skills/kh/templates/intent.md`). The runtime refuses a missing or
draft record before planning. Capture the originator's intent and owner
acceptance before starting.

The controller resolves routing, prepares missing project context, captures scope, independently reviews the plan, builds, runs configured
checks and requests an independent review with bounded repairs. Missing required
checks are setup errors. A dry-run reports workflow readiness without dispatching workers;
never label it a completed model run. For live execution, report the actual run
ID, profile, requested/effective routing, scope and verification outcomes.
Project context preparation uses project_architect and plan_reviewer. Planning uses
planner and plan_reviewer in independent phase contexts: at most two review
rounds. The second review is final, and its remaining findings bind the builder
and the code reviewer instead of reopening planning. Missing role choices are setup errors; never
silently reuse another role. Plan approval alone does not complete the task.
Use `--refresh-context` to review and refresh stale context before planning.
Full mode is the default. `--dry-run` reports the actual budget under `execution`;
use that preview and the selected configuration rather than a copied default.
Phase repairs and output corrections stay within its total ceiling and deadline. Report consumed limits and stop
reasons from actual runtime evidence. For deliberately small tasks, the existing
/kh-simple-sdlc is an alias for /kh-start --mode light; do not switch it silently.
Do not launch the command again while the original process is still running.

## User arguments

$ARGUMENTS
