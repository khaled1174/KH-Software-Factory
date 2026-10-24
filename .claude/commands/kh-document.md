---
description: "Document the change against a Git reference."
argument-hint: "[--config PATH] [--adw-id ID] --base REF <task or task-file>"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[standard execution contract](../skills/kh/references/execution-contract.md#standard-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_document.py`.
Base argv: `["uv", "run", "adws/adw_document.py"]`.
Purpose: Document the change against a Git reference.
Effects: Writes documentation; default comparison reference is main.

Supported leading options: --config PATH, --adw-id ID, --base REF.
Use the explicit --config first, otherwise KH_CONFIG if set, otherwise
adws/adw_kh_config/kh.config.yaml. Pass that selected path with --config.
Default --base is main. Verify the requested Git reference exists; do not create it.

Pass options as separate argv entries, then --, then the task as ONE positional
argument. A supplied run ID joins or pins the session; it does not skip the
workflow's earlier phases. Do not choose a previous run ID automatically.

If no task is supplied and the conversation does not identify one unambiguously,
ask one short question for the task; do not invent implementation work.

## User arguments

$ARGUMENTS

