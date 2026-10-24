# Project tasks and milestones

The installed `project-plan.example.json` shows schema version 1. Prepare the
owner's complete plan with project id/name, milestones id/title and tasks with
id, milestone_id, title, request, deliverables and nonempty allowed_paths.
Optional dependencies reference task IDs; constraints, non_goals, required_checks
and positive weight are supported. Cycles, duplicate/unknown IDs, unsafe paths
and unknown fields are rejected before initialization.

```bash
uv run adws/adw_project.py init --plan project-plan.json --dry-run
uv run adws/adw_project.py init --plan project-plan.json
uv run adws/adw_project.py next
uv run adws/adw_start.py --task T1 --dry-run
uv run adws/adw_start.py --task T1
uv run adws/adw_project.py run-continuous
uv run adws/adw_project.py status
```

Initialization is once per repository; it does not silently replace an existing
plan or merge newly invented tasks. Start tasks explicitly. The current runtime
holds one repository controller lock and runs planner, builder, quality commands
and one independent combined reviewer sequentially. Execution is sequential.

Historical `.kh/parallel/`, execution-decision and execution-metadata records
are preserved but rejected before dispatch with `LEGACY_PARALLEL_STATE`.
Reconcile incompatible saved state explicitly; it cannot authorize a new run.

`run-continuous` keeps the recorded plan finite and unchanged during a run. It selects one ready
task, runs the normal reviewed controller, records acceptance, emits a completion
event for the owner, and only then selects the next task from current ledger
state. Stale context receives an independently reviewed refresh under the same
lock and role choices before planning. A rejected refresh or unresolved owner
decision stops the run. Blocked work is never skipped
or retried automatically. `pause-after-current` stops safely between tasks, and
a later `run-continuous` resumes from the next pending ready task. The cooperative
state is stored in `.kh/controller-state.json`; the SQLite project ledger is
still authoritative for task completion.

Use `amend --task ID --definition PATH --reason TEXT` to replace one pending or
blocked task from a complete JSON task object. Use `split --task ID --plan PATH
--reason TEXT` with a JSON list of replacement task objects; the first retains
the original ID. Every new request needs an already accepted matching intent.
Revisions refuse completed tasks and any active project task. Split retains the
original milestone and total weight, narrows path scope, inherits prerequisites,
constraints, non-goals and checks, and rewires downstream dependencies to every
terminal replacement. `--dry-run` validates without writes. A real revision uses
one SQLite transaction, makes affected tasks pending, and retains previous state,
attempts and evidence in history. Saved candidates stay unchanged until normal
resume validation. These commands never write acceptance on the owner's behalf.

Each agent returns a typed summary and evidence. The controller immediately
records the handoff, updates task reports, and records running/verifying/reviewing
or needs_repair transitions. Handoff is not task acceptance. Only unchanged
verified inputs, approved review and successful source promotion produce a saved
receipt and completed ledger state. Failure records blocked with its reason;
unfinished dependencies cannot start. Retry a resolved blocked task with a new
run ID. A killed controller leaves an active task requiring explicit `recover`;
recovery marks it blocked and never guesses that changes were accepted.

`.kh/project.db` is authoritative. Derived files are
`.kh/project-status.json`, `.kh/PROJECT_STATUS.md` and
`.kh/task-reports/ID.md`; receipts are in `.kh/task-receipts/ID/RUN.json`.
These local runtime records are ignored by Git. Preserve/backup the ledger and
receipts for project history; reports alone cannot resume the project. Use
`adw_project.py report` to regenerate reports after an interrupted refresh.
Reports use atomic individual writes; the multi-file set is not one transaction.

Progress is accepted completed weight / total task weight, per project and
milestone. Pending, blocked and active tasks count zero. Default weight is 1.
Show completed/total task counts, blockers and ready tasks beside percentages.
The visualizer Project view reads the snapshot even before the first model run.
Start it with `just obs`, or provide `KH_PROJECT_ROOT` explicitly when using a
custom trace database location. Viewing reports never starts workers.

No token/spend budget or automatic external documentation publication is added.
Provider sessions and the project ledger are separate; project receipts retain
run IDs and evidence links. Local trusted controller APIs enforce workflow order;
they are not an OS sandbox against arbitrary code with the owner's privileges.
