---
description: "Prepare reviewed project navigation and rules."
argument-hint: "[--config PATH] [--profile NAME] [--dry-run] [--refresh]"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[guarded execution contract](../skills/kh/references/execution-contract.md#guarded-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_bootstrap.py`.
Base argv: ["uv", "run", "adws/adw_bootstrap.py"].
Supported options: --config PATH, --profile NAME, --dry-run, --refresh.

Use the owner's explicit project_architect and plan_reviewer choices. The architect
proposes documentation; the independent reviewer reviews it. Only the controller
publishes validated documentation. Preserve existing owner instructions and source.
A dry-run reports readiness without calling models or writing files. --refresh is
an explicit request to reassess existing context; do not refresh on every task.
Report actual blockers and the context receipt. Context readiness is not feature
completion or proof that project quality tooling is installed. Do not execute
suggested hook/tool installation as part of documentation preparation.

## User arguments

$ARGUMENTS
