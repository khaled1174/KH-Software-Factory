---
description: "Show phases for a run."
argument-hint: "<run-id> [--db PATH]"
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[standard execution contract](../skills/kh/references/execution-contract.md#standard-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Run the installed standard-library helper from the project root:
["python3", "adws/kh_commands.py", "phases"]

Support --db PATH as separate argv entries. The default database is
adws/adw_data/kh.db. The helper uses SQLite read-only mode and bound SQL
parameters. It never initializes, updates, or creates the database.
Pass a supplied run ID as ONE positional argument. Put --db PATH before
-- RUN_ID so a run ID beginning with a dash remains literal data. With no ID,
the helper selects the most recent recorded run and includes its ID in output.
Display JSON rows as a concise table. This is a single snapshot. Process rows
show recorded state and do not independently prove that a PID is alive.
Do not issue SQL writes or signals. Report missing databases or helper files.

## User arguments

$ARGUMENTS

