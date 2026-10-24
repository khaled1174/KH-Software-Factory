# Project acceptance recipes

Use the existing visual/interaction/E2E verification adapters with a real
project-owned harness. Configure only commands that exist. KH installs no browser,
test framework, database or credentials for these examples. Source changes and
unknown/missing results still fail the normal controller contract.

The installed `adws/adw_modules/acceptance_recipe.py` is a standard-library result
formatter. It executes no tests, cleans up no resources, accepts no baselines and
makes no acceptance decision. Import it from an actual harness after assertions:

```python
import os
from adw_modules.acceptance_recipe import emit
# Capture this BEFORE executing tests; never relabel an older report as current.
candidate = os.environ['KH_CANDIDATE_SHA256']
# Run the project's real tests, collect assertions/captures, stop owned work,
# await cleanup, then pass those actual observations. This example is a schema,
# not a passing test or a ready project implementation.
exit_code = emit(
    candidate_sha256=candidate,
    discovered_tests=actual_discovered_count,
    cases=actual_executed_cases,
    evidence=actual_relative_artifact_paths,
    cleanup=actual_owned_resource_observations,
    unresolved_differences=actual_unresolved_difference_count,
)
raise SystemExit(exit_code)
```

Each executed case contains `name`, a concrete `requirement`, `passed` as a bool,
positive `assertions` and `surfaces` naming the source areas that case actually
exercised. Never copy every changed path into coverage by default. Zero execution,
failed/no assertions, partial coverage and stale identities fail. The controller
still checks the output schema, evidence files and unchanged candidate itself.
`recipe-cases.json` retains case and cleanup details beside the result.

## UI: export the actual result

Use the project's existing browser runner and isolated test account/data. Assert
the initial controls, trigger a real download, parse the downloaded CSV and compare
its rows with a literal known fixture. Repeat with an empty result and assert that
headers remain. Capture the critical result and retain the downloaded content as
additional evidence. A visible button or screenshot alone cannot pass this case.

Map each test to the actual component/API surfaces it exercised. Record browser
assertions from the runner, not a model's claim. Close pages, contexts, browser
instances and test servers owned by the harness before emitting its result. For
visual baselines, keep unresolved differences visible; this helper never accepts
a new baseline or silently sets the count to zero. Use actual project selectors
and URLs; the framework does not invent them.

## Backend: dependency unavailable

Call the application's real public seam using the existing guarded test DB
connection forms and process-local values. Assert the application's specified
failure status/message, bounded completion and absence of partial writes. A test
of the DB driver alone does not prove the application's failure behavior. Restore
process-local state in `finally`; do not alter server, container or DB settings.

When testing command cancellation, retain the exact child handle and observe its
termination. Close and await owned readers, writers and connections. Inject failure
after work starts and assert cleanup, not merely detection of a timeout. Never use
process-name-wide kills, a different account or a permission bypass to obtain a pass.

`cleanup` has exactly `confirmed` (true), `active_children` (0), `open_readers` (0)
and `open_connections` (0). These are observations of resources owned by this
harness. Unknown cleanup or nonzero counts fail. They do not attest that unrelated
services or every resource on the host stopped. Add other resource assertions as
cases/evidence; do not hide an untracked resource behind these counters.

## Wiring and readiness

Place the harness in the project's existing tests/scripts area and import the
installed `adw_modules` using the project's Python path. Run its own positive and
negative cases before enabling its argv in `defaults.verification_adapters`.
Confirm real discovery, an intentionally failed assertion, missing capture,
stale candidate, partial surface coverage and cleanup failure all fail. The
controller supplies `KH_VERIFICATION_RESULT`, `KH_CANDIDATE_SHA256` and
`KH_CHANGED_PATHS`. Output is a fresh controller-owned directory; evidence is
relative to it. Existing evidence is never overwritten.

Use the project's configured Python environment. The formatter adds no package
dependency. Native worker test commands remain subject to the existing capability,
capture-command and infrastructure checks before building. An adapter being
configured is not proof that its application or browser dependencies are ready.
