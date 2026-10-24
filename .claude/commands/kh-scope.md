---
description: "Show the controller-owned scope for an KH run."
argument-hint: "[RUN_ID] [--config PATH]"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[guarded execution contract](../skills/kh/references/execution-contract.md#guarded-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_scope.py`.
Base argv: ["uv", "run", "adws/adw_scope.py"].
Supported options: --config PATH; optional positional RUN_ID.

Run the read-only script and display its selected run ID, original request,
deliverables, constraints, non-goals, allowed paths and required verification.
When no ID is supplied, report the run selected by the script explicitly.
A missing scope is a reported absence, not permission to invent or create one.
This command does not change scope, accept work, restart workers or dispatch a
model. Legitimate owner-directed changes belong in an explicit revised scope
for a new guarded run, not an edit to an active controller's evidence.

## User arguments

$ARGUMENTS
