---
description: "Run guarded light planning, independent review, implementation, and checks."
argument-hint: "[--task ID] [--dry-run] [--profile NAME] [--use ROLE=RUNTIME:MODEL:EFFORT ...] <task or task-file>"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[standard execution contract](../skills/kh/references/execution-contract.md#standard-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_simple_sdlc.py`.
Base argv: `["uv", "run", "adws/adw_simple_sdlc.py"]`.
Purpose: Run the guarded light workflow: reviewed planning, implementation,
mandatory deterministic checks, independent code review, and accepted promotion.
Effects: Controller handoffs and receipts document results. This command is an
alias for /kh-start --mode light.

Supported leading options: --config PATH, --adw-id ID, --profile NAME,
--use ROLE=RUNTIME:MODEL:EFFORT (repeatable), --scope-file PATH, --dry-run,
--save-profile, --task TASK_ID.
For --task, use the ledger's frozen request and scope, omit positional task text,
and do not combine with --scope-file. The mode defaults to light. `--dry-run` reports its actual budget under
`execution`, including repair allowances, send ceiling and deadline. Output
corrections consume the same send ceiling. Project documentation preparation
has a separate reported budget. All configured
mandatory checks remain required. Never silently change mode or model choices.
Use the explicit --config first, otherwise KH_CONFIG if set, otherwise
adws/adw_kh_config/kh.config.yaml. Pass that selected path with --config.

Pass options as separate argv entries. For an ordinary request, append -- and
the task as ONE positional argument; for --task, do not append positional text. A supplied run ID joins or pins the session; it does not skip the
workflow's earlier phases. Do not choose a previous run ID automatically.

If neither --task nor task text is supplied and the conversation does not identify one unambiguously,
ask one short question for the task; do not invent implementation work.

## Model choice for this run

Before launching a live guarded run, ask the owner which model and effort each role
uses, following `.claude/skills/kh/references/model-choice.md` (read it
first). In short: run `["uv", "run", "adws/adw_models.py", "--choices"]`
(read-only), ask with AskUserQuestion one question per role (current choice
first, only the listed options, a typed RUNTIME:MODEL:EFFORT allowed), then add
`--use ROLE=RUNTIME:MODEL:EFFORT` for each changed role. Ask once, before the
run, never during it. Skip asking when the request already names the models,
the owner said to keep the current setup, or no one can answer (then use the
current setup and say so). Answers apply to this run only.

## User arguments

$ARGUMENTS

