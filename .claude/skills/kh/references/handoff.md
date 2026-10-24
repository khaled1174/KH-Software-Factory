# Typed evidence and handoff

Source of truth: adw_modules/data_types.py. Keep schemas, role prompts and
call-site output_type aligned when extending a role.

Every envelope has status, summary, artifacts, notes_for_next_agent and optional
observations. status describes worker completion; the controller owns acceptance.

- PlanOutput: nonempty acceptance_criteria plus plan evidence.
- BuildOutput: exact changed_files, including deletions.
- ScoutOutput: findings plus report evidence.
- ReviewOutput: approved, blocking and findings covering controller requirements
  exactly, each with concrete evidence, plus a review artifact.
- DocumentOutput: document_path must identify a declared nonempty artifact.
- GenericOutput: task-dependent output; do not substitute it for a required role.

Artifacts resolve inside the isolated workspace and are mapped to project paths
after promotion. Each phase receives a fresh context_handoff_dir. Read prior
artifacts from previous_envelope; do not assume the plan is in your output folder.

Immutable records live under:
- sessions/<run>/<agent>/phases/<phase>/prompts/
- sessions/<run>/<agent>/phases/<phase>/attempt-<n>/
- sessions/<run>/<agent>/phases/<phase>/envelope-<unique>.json
- sessions/<run>/context_handoff/<phase>/<agent>/

Observations contain title, issue, suggestion, principle, skill and optional
project-relative evidence paths. The controller supplies run/phase/revision
provenance. They do not mutate current task criteria or active instructions.
