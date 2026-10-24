---
description: "Show the available KH slash commands."
argument-hint: ""
disable-model-invocation: true
---

## Action

Resolve the target project from the current session: use its Git root or closest
unambiguous ancestor containing adws/. Read the canonical command catalog at
.claude/skills/kh/references/commands.md relative to that project root, including
when this command was installed at user scope. It is the installed copy of
COMMANDS.md from the distribution.

Display that catalog in the user's language. Do not execute a workflow, query
project state, install anything or infer approval from examples. If the reference
is missing, report that this project needs the KH project installation; global
command installation alone does not supply a runtime. Do not reconstruct an
outdated catalog from memory.

## User arguments

$ARGUMENTS
