---
description: "List the last ten KH runs."
argument-hint: "[--db PATH]"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[standard execution contract](../skills/kh/references/execution-contract.md#standard-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Run the installed standard-library helper from the project root:
["python3", "adws/kh_commands.py", "sessions"]

Support --db PATH as separate argv entries. The default database is
adws/adw_data/kh.db. The helper uses SQLite read-only mode and bound SQL
parameters. It never initializes, updates, or creates the database.
Display JSON rows as a concise table. This is a single snapshot. Process rows
show recorded state and do not independently prove that a PID is alive.
Do not issue SQL writes or signals. Report missing databases or helper files.

## User arguments

$ARGUMENTS

