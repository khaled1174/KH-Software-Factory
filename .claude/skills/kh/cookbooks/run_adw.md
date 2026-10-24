# Execute a workflow

Run from the target project root. Preserve the named workflow, model roster and
user authorization. Pass task text as one safely quoted argument or task-file path.

```bash
uv run adws/adw_scout.py -- "Explain the project"
uv run adws/adw_plan_review.py --adw-id feature-001 -- "Plan the feature"
uv run adws/adw_start.py -- "Implement the agreed plan"
```

/kh-help lists all 18 slash shortcuts. simple-sdlc is the light guarded route:
plan, plan review, build, configured checks, independent code review. Use
`--dry-run` to read the actual budget under `execution`, including send ceilings,
repair allowances and deadlines. Missing project context has a separate disclosed
bootstrap budget. Default values live in `../references/config.md`.

No automatic fallback or escalation occurs. Existing owner model
choices and mandatory checks are preserved. Guarded run IDs cannot be reused.
If the owner asks conversationally to execute the whole recorded project,
review its overall plan and ticket decomposition, then use
`adws/adw_project.py run-continuous`. It emits `KH_TASK_COMPLETED` after each
accepted task and supports `pause-after-current`. Relay completion events
immediately. Execution is sequential. Historical parallel state is refused;
retain it for reconciliation with the prior release.
Do not ask for a task ID or count when the ledger and required setup are ready.
The legacy auto_commit key is accepted without effect. Native retries
stay within a phase and count against the guarded global correction allowance.

Use the actual background process handle and output to monitor long runs.
Read-only snapshot commands:
python3 adws/kh_commands.py sessions
python3 adws/kh_commands.py phases RUN_ID
python3 adws/kh_commands.py tail RUN_ID
python3 adws/kh_commands.py procs RUN_ID

Trace PIDs are recorded state, not independent liveness proof. Use the launcher's
process handle for cancellation. No just kill or just rosters recipe is included;
read the actual config files for a roster. Do not invent those recipes.

Report workflow/config, run ID, phase outcomes, errors and acceptance separately.
Do not rerun a different workflow to conceal failure.
