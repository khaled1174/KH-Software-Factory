# Task scope input

Save a JSON object and pass its path with `--scope-file`. This is project input,
not a global policy override. Choose paths and check names from the real project.
For example, in a project whose test and typecheck commands are configured:

```json
{
  "original_request": "Fix sign-in so a valid user can open their account",
  "deliverables": ["Valid credentials open the account", "Invalid credentials show an error"],
  "non_goals": ["Redesign the application", "Change the database schema"],
  "constraints": ["Preserve existing authenticated sessions"],
  "allowed_paths": ["src/auth/**", "tests/auth/**"],
  "required_checks": ["test", "typecheck"],
  "review_required": true
}
```

Keep original_request consistent with the actual request supplied to startup.
Planner and reviewer artifacts use their controller-supplied phase evidence directories;
source path permissions do not need to include a separate specs directory.
Existing protected controls remain protected regardless of allowed_paths.
`required_checks` adds to project requirements and cannot remove configured
required checks. Guarded startup requires independent review and rejects
`review_required: false`. A
broad path such as `src/**` still does not authorize a new feature unrelated to
the stated deliverables. Review both file scope and behavioral relevance.

For a sign-in fix, adding a regression test or changing a helper inside the
allowed paths can be necessary supporting work. Adding profile customization
is deferred. If a necessary change falls outside permitted paths or contradicts
an explicit non-goal, revise the scope before dispatch rather than overriding
the restriction inside a worker prompt.

`allowed_paths` is optional in the file. When omitted, the accepted plan must
declare `allowed_paths` and the controller freezes those as the builder's
boundary (`effective_scope.json`), so a guarded run is never unbounded. When
supplied, the plan may only narrow them; a plan proposing a path outside the
owner's list fails before any build. Plan *patterns* are accepted only when
containment is provable (identical to an owner pattern, or under an owner
directory boundary such as `src/` or `src/**`); otherwise declare concrete
files. At runtime every changed file must satisfy both the owner boundary and
the plan boundary (`plan_paths` in `effective_scope.json`). `scope.json`
always keeps the owner's original contract; `plan_paths` is controller-owned
and not accepted in a scope file.

Use `/kh-scope RUN_ID` or `uv run adws/adw_scope.py RUN_ID` to inspect the
controller's stored task scope and, once planning ran, the effective scope. Omitting the run ID selects the latest scope
using the runtime's documented selection. Display the selected run explicitly;
this read-only command neither widens its scope nor restarts its workers.

For owner-directed expansion, use an explicit updated scope and a new run unless
the runtime supports a validated scope revision. Never edit an active run's
controller-owned scope artifacts to make an already unauthorized delta appear
accepted. Preserve the original request, the user's revision and the reason.
