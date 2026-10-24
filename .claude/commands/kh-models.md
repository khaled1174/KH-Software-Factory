---
description: "List configured KH model profiles and local prerequisites."
argument-hint: "[--config PATH] [--doctor] [--choices [--profile NAME]]"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[guarded execution contract](../skills/kh/references/execution-contract.md#guarded-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Mapped script: `adws/adw_models.py`.
Base argv: ["uv", "run", "adws/adw_models.py"].
Supported options: --config PATH, --doctor, --choices, --profile NAME.
--choices prints, per role the next run needs, the current choice and up to four
configured options. The run commands use it to ask the owner before a run.
A model name is an owner choice, not a capability proof. Choices are resolved
before a run and remain fixed for that run; persistent setup waits until idle.

Run this read-only listing. --doctor adds local transport diagnostics without
authentication, provider catalog lookup, or remote execution. Display each configured profile's role assignments
and fallback candidates and the local prerequisite findings returned by the
script. Distinguish configured, locally detected and authenticated/live-tested.
Doctor also reports structural role/runtime capability compatibility; it does
not execute Swift/database probes or establish native sandbox/network access.
No provider login, remote catalog query, model execution or config write is
part of this action. Do not promise a model is usable because its name appears.

## User arguments

$ARGUMENTS
