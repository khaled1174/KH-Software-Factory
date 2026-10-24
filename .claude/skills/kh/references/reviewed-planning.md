# Reviewed planning

Guarded start uses one controller and independent phase contexts:
context preparation when missing → plan → plan review → revision when required →
build → deterministic checks → code review → bounded code repair.

## Choose the roles

Run `/kh-setup-models --interactive --apply` and explicitly configure
`project_architect`, `planner`, `plan_reviewer`, `builder`, and `reviewer` with
available runtime/model/effort combinations. The command does not invent model
identifiers or prove provider authentication. Existing partial settings remain
readable; a workflow reports missing required choices before model dispatch.
Planner and plan reviewer can use the same model by owner choice, but never the
same conversational phase context. Plan review is separate from the code-review
panel and cannot replace it.

## Commands and approval

- `/kh-plan-review "request"`: reviewed plan only, with prepared context.
- `/kh-start "request"`: complete guarded workflow; creates a current reviewed plan.
- `/kh-start --task T1`: same workflow with the immutable imported task contract.

There is no `--use-plan` shortcut. A standalone plan does not authorize execution.
Do not change the imported project task ledger when revising an implementation
plan. Plan approval cannot complete a project task or raise project progress.

The planner submits actionable steps, scope and requirement coverage. The
controller validates the plan and gives a separate reviewer the exact plan,
original request, context and previous findings. Corrections return to the
planner. Each revised version requires another review. Approvals bind exact
content; a stale review is rejected. Operational failures are not approvals.

Planning has at most two review rounds. The planner writes the plan and the
reviewer reviews it. The planner revises once, and the reviewer reviews again.
That second review is final: if it still has findings, they do not reopen
planning. The plan is approved with them as binding notes. The builder receives
them with the task, and the code reviewer must rule on each one ("Final plan
review note …") like any other requirement. The planner is shown the exact open
finding IDs to answer. A missing or extra answer is passed to the reviewer as
`unanswered_finding_ids` instead of costing a round. `max_plan_repairs` 0 keeps
a single, final review; any value of 1 or more means the two rounds above. `max_bootstrap_repairs` is independently bounded;
`max_repairs` remains the code-repair budget. An unchanged plan, an owner
decision, a structurally invalid final revision, or invalid output blocks
execution. No model can
relax frozen owner scope to close a finding. A scope/plan change needed during
implementation requires a new reviewed run rather than a nested unlimited loop.

Step order: a step may use only what the tree or an earlier step provides. The
controller checks this deterministically before each review: a concrete path
new to the tree is created by the first step that lists it, and an earlier step
whose text names that path, or its file stem as a whole word (`FakeExtractor`
for `FakeExtractor.swift`), is listed in `controller_order_checks` (recorded as
planning/order-N.json). The reviewer is told to raise a finding for each or
dismiss it with a reason; nothing enforces its answer. It is a hint, not a block: a type named differently from its
file is not detected.

Re-running a task: a new run whose request, scope, context, intent, planner and
plan-reviewer settings, mode and source are unchanged reuses the plan an
earlier run approved (digests rechecked, plan validated again, evidence copied,
new approval with `reused_from`). Any difference plans from scratch;
`execution.reuse_approved_plan: false` always does.

## Evidence and compatibility

Read the run trace for each phase's owner, actual model, output, findings and gate
results. Versioned artifacts and the controller approval retain exact plan
identity. A reviewer's approval recommendation is not itself a controller receipt.
Historical traces without these records remain readable and must not be labeled
approved by inference. Completion still requires checked/reviewed source promotion
and the final task receipt, including the new plan-approval reference.

Use `--dry-run` before first execution to inspect readiness without worker calls
or writes. Configure real `test`, `lint`, `typecheck`, and `build` commands as
applicable. New plan review does not turn placeholder checks into verification.

## Corrections adapted from UI Verify

The supplied [article](https://uiverify.ai/blog/stop-writing-storybook-stories-by-hand)
and [skills](https://github.com/uiverify/uiverify/tree/main/packages/skills/skills)
provide source ideas for modular rules, independent review and visual verification.
KH remains the sole controller. No second factory scheduler, automatic baseline
acceptance, personal-browser-session extraction or remote publishing is added.

Applicable visual/interaction/E2E evidence must identify the candidate, covered
surfaces and explicit result. Empty stories, undiscovered tests, unknown statuses
and preview exit 0 alone cannot prove coverage. Projects supply their actual stack
adapter; KH does not automatically install Storybook or another test framework.
Rule/guard suggestions become scoped tasks, not automatic edits to active rules.

## Optional project verification adapter

Configure only adapters that exist in the project. Example configuration (the
project must implement this script before enabling the entry):

```yaml
defaults:
  verification_adapters:
    - name: ui
      kind: visual
      applicable_paths: ["src/**"]
      argv: ["python", "scripts/verify_ui.py"]
      timeout_seconds: 120
```

Kinds are `visual`, `interaction`, or `e2e`. The controller supplies
`KH_VERIFICATION_RESULT` (output JSON path), `KH_CANDIDATE_SHA256` (exact
candidate identity), and `KH_CHANGED_PATHS` (JSON applicable changed paths).
The adapter writes this schema, using actual results rather than this example's
illustrative values:

```json
{
  "schema_version": 1,
  "candidate_sha256": "value from KH_CANDIDATE_SHA256",
  "status": "passed",
  "affected_surfaces": ["src/example.tsx"],
  "covered_surfaces": ["src/example.tsx"],
  "evidence": ["capture.png"],
  "discovered_tests": 1,
  "executed_tests": 1,
  "unresolved_differences": 0
}
```

`affected_surfaces` must equal the supplied changed-path set; coverage must cover
it. Discovery and execution counts must be positive, with execution no greater
than discovery. Evidence paths are relative to the output JSON directory and must
resolve to regular nonempty files without symlinks. The candidate digest must
match. Unknown status, incomplete coverage, missing evidence or unresolved visual
differences cannot pass just because the command exits 0. Verification cannot edit
source; failed evidence is retained for repair/review. An empty adapter list makes
no visual/E2E coverage claim. Enabling adapters installs no libraries automatically.
