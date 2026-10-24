# Project observations

Storage uses `.kh-observer/observations.sqlite3` under the explicit project root.
Use local disk with reliable SQLite locking; shared/network filesystems are not a
supported concurrent database target. Back up using SQLite backup, not a copy while
writes are in progress. A UUID is the stable ID; legacy numeric IDs remain archival.
SQLite transactions serialize concurrent writes, uniqueness constraints prevent ID
collisions, and version checks reject stale status edits.

The controller imports `scripts/observer_store.py` with importlib or invokes the CLI:

```sh
python3 .claude/skills/task-observer/scripts/observer_store.py --project "$PWD" list
python3 .claude/skills/task-observer/scripts/observer_store.py --project "$PWD" record --json observation.json
python3 .claude/skills/task-observer/scripts/observer_store.py --project "$PWD" status UUID actioned --version 1
```

The record JSON has nonempty strings `title`, `issue`, `suggestion`, `principle`,
`skill`, `run_id`, `phase`, `source_revision`; `evidence` is an optional list of existing
project-relative file paths. Source revision is supplied from the controller's pinned
baseline, never inferred from worker claims. Evidence bytes are hashed. Worker results
may carry title/issue/suggestion/principle/skill; the controller supplies provenance.

Python API: `record_observation(project_root, **record)` returns ID, version, status,
and deduplicated flag. `list_observations(project_root, status=None)` includes occurrences.
`set_status(project_root, id, status, expected_version)` detects conflicting updates.
Statuses: open, actioned, declined, superseded, parked. Actioned means the observation
was handled; it does not establish a task passed or a skill was installed.

Raw text and evidence references never enter shared knowledge automatically. Explicit
promotion requires separately authored sanitized content and a reviewer:

```sh
python3 .claude/skills/task-observer/scripts/observer_store.py --project "$PWD" promote UUID --shared-root /chosen/shared-lessons --lesson-file sanitized.txt --reviewed-by reviewer-id --confirm-sanitized
```

The export has only sanitized lesson, reviewer, timestamp, source UUID and opaque source
hash. It does not copy issue, title, source paths, skill, evidence or run data. Sanitization
is an explicit reviewer decision, not an automated guarantee: inspect the supplied text.
This store does not load another project's database or automatically retrieve shared lessons.
