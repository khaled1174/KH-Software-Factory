---
description: "Create, independently review and revise a plan without building."
argument-hint: "[--config PATH] [--profile NAME] [--scope-file PATH] [--dry-run] [--adw-id ID] <TASK>"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[guarded execution contract](../skills/kh/references/execution-contract.md#guarded-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_plan_review.py`.
Base argv: ["uv", "run", "adws/adw_plan_review.py"].
Supported options: --config PATH, --profile NAME, --scope-file PATH, --dry-run,
--adw-id ID.

The runtime requires a matching accepted `intents/*.md` record before planning
or dry-run. See `.claude/skills/kh/templates/intent.md` for the exact request and acceptance fields.

Preserve the task as ONE positional argument after --. If no task is unambiguously
supplied, ask for it. Use prepared project context and the owner's explicit planner
and plan_reviewer choices. The controller bounds revisions and binds approval to
the exact plan version. Report the reviewed plan or exact blocker and run ID.
Never treat an approved plan as implemented work. This command does not build,
complete a project task, or authorize a later execution. /kh-start creates and
reviews its own current plan; there is no --use-plan shortcut. Dry-run does not
call a model, create an approved plan, or publish context.

## User arguments

$ARGUMENTS
