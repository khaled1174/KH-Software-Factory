# Plan task

Request:
{{prompt}}

Previous context:
{{previous_envelope}}

Current phase evidence directory:
{{context_handoff_dir}}

Read the relevant source and write plan.md in this evidence directory. State
bounded files, behavioral changes, non-goals and verification. Preserve every
controller requirement. A planner cannot expand authorized paths or replace
an explicit non-goal. Report a necessary unresolved scope change to the
controller before implementation.

Only if this is an unscoped legacy planning workflow explicitly requesting a
specs copy, copy the finished plan to a new allowed specs filename. Scoped
startup requires all declared artifacts in the current evidence directory.

Return only JSON matching the controller-supplied output schema, with status, summary, artifacts containing the
actual plan path, nonempty acceptance_criteria, nonempty allowed_paths, and
notes_for_next_agent.

`allowed_paths` is the exact list of source files or patterns this task needs
to change (for example `["src/auth/login.py", "tests/auth/test_login.py"]`).
The controller freezes it as the builder's write boundary: when the owner
supplied allowed_paths, yours must fall inside them; when the owner supplied
none, yours become the boundary. Declare what the task needs, not the whole
repository. Prefer concrete file paths; a pattern is only accepted when it
provably lies inside an owner directory boundary such as `src/` or `src/**`.

When the controller requests ReviewedPlanOutput, provide concrete dependency-ordered steps, requirement-ID coverage, required checks, assumptions/risks, and explicit responses to prior findings. Never approve your own plan. Every revision returns to the independent plan reviewer.
