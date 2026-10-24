---
description: "Run the configured quality commands."
argument-hint: "[--config PATH] [--adw-id ID] <task or task-file>"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[standard execution contract](../skills/kh/references/execution-contract.md#standard-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_quality.py`.
Base argv: `["uv", "run", "adws/adw_quality.py"]`.
Purpose: Run the configured quality commands.
Effects: No model worker; check commands may produce build artifacts. Quality commands must be configured for this project before acceptance.

Supported leading options: --config PATH, --adw-id ID.
Use the explicit --config first, otherwise KH_CONFIG if set, otherwise
adws/adw_kh_config/kh.config.yaml. Pass that selected path with --config.

Pass options as separate argv entries, then --, then the task as ONE positional
argument. A supplied run ID joins or pins the session; it does not skip the
workflow's earlier phases. Do not choose a previous run ID automatically.

If the task is empty, use "Run the configured project quality checks".

## User arguments

$ARGUMENTS

