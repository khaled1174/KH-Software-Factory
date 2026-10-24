# Skills integration register

## Components and responsibilities

KH Software Factory has one acceptance controller, 18 slash commands and 12
mapped scripts. Execution is sequential. Supported transports are Claude Code
and the pinned Codex relay. Saved role choices belong to the owner.

| Component | Responsibility | Boundary |
| --- | --- | --- |
| kh | Planning, dispatch, scope, checks and acceptance | Sole controller; supporting skills cannot approve tasks. |
| kh-scope-guard | Scope and evidence guidance | No scheduler or permission authority. |
| task-observer | Local observations and explicit sanitized promotion | No automatic rule activation or external scheduling. |
| fable-pack-audit, astra-pack-audit | Instruction audits and proposed diffs | No automatic edits or task acceptance. |
| Codex relay | Explicit sandboxed CLI transport | Source pin and MIT license in vendor/delegate-skills. |
| Role prompts | Task-kind, review and documentation guidance | No independent scheduler, tool grants or approval. |

## Current integration rules

The controller owns findings, intent acceptance, source fingerprints and final
promotion. Reviews must account for earlier blocking findings. Documentation
uses current guides in docs/agent-guide/ and project change records in app_docs/.
Only reviewed active guides enter task context. Cost remains informational.

The installer preserves differing owner configuration and prompts. Standalone
commands resolve only their required saved roles. Model setup requires an idle
repository; a running task retains its resolved model choices. Unsupported
provider choices fail explicitly and require /kh-setup-models.

The observer uses an owner-controlled local store. Sanitized promotion is
explicit. Neither audit skill can install a scheduler, become a router or
accept a task. Frontend trace views have no execution authority.

## New-skill adoption checklist

Record the repository and pinned revision; intended responsibility; required
tools and credentials; trigger/context cost; reads, writes and external effects;
scheduler/router/approval overlaps; project-data handling; stable findings and
reproductions; compatibility tests; adoption decision; and implemented revision.
Preserve the owner's existing authorization. Do not activate reviewed
instructions merely because they appear in source material. New components
must not independently accept the same task or bypass current permissions.

## Source and validation limits

[THIRD_PARTY.md](THIRD_PARTY.md) identifies copied sources, conceptual influences
and retained licenses. [VERIFICATION.md](VERIFICATION.md) states current checks
and known limits. Offline contract tests do not establish general model quality,
subscription billing or external-tool availability on an arbitrary host.
