# Task review and delivery improvements

## Outcome

A project owner can open a stopped task from **Needs attention**, inspect the exact attempt, send a correction, and see the result of the next attempt on the same page. A single-task retry enters the existing guarded workflow. The controller owns dispatch to roles, checks, independent review, source promotion, and the acceptance receipt.

The first release covers tracked project tasks on the owner's local machine. It adds a read-only review page first, then an opt-in path for sending feedback and starting a new attempt. The release includes a demonstration with deterministic fixtures. Workflow templates and delivery export follow in separate changes.

## Current contracts

| Area | Existing behavior and implementation seam |
| --- | --- |
| Project view | `ProjectOverview.vue:118` lists tasks that need attention. `server/index.ts:128` reads the optional `.kh/project-status.json` projection. Use these for discovery and display. |
| Routes and presentation | `src/lib/router.ts` and `src/App.vue` route the project and run screens. `SessionTrace.vue` renders run history. Reuse the existing hash navigation, trace links, and request handling in `src/lib/api.ts`. |
| Attempt history | `adw_modules/project.py:491` records the new attempt and clears the task's current summary and blocker. The ledger retains earlier events and run IDs. Join evidence and comments to their own attempts. |
| Evidence | `adw_start.py:445` runs checks. `_measure_delta` at line 476 writes the controller patch after checks pass. `adw_modules/evidence.py:123` publishes handoff files on success and failure. Its `artifact-map.json` lists artifact paths. Compute identities from the bytes served. |
| Kept work | `adw_modules/resume.py:214` validates stored work. The `.kh/work/<task>/current` directory can be replaced or cleared. Use it to validate reuse at start. Serve historical evidence from the selected run's published artifacts. |
| Controller entry | `adw_start.py:955` accepts `--task` and requires the recorded request. `adw_modules/session.py:66` provides the repository lease. `_resume` and `_scope` prepare the candidate before the first builder call. |
| Owner decisions | `OWNER_REVIEW_REQUIRED` in hold mode uses the task's re-accepted intent. `OWNER_DECISION_REQUIRED` can publish a decision draft for the existing dispositions contract. `--review-existing` handles eligible checked work without a builder round and can promote after independent approval. `--advisory` is the explicitly nonpromoting review mode. |
| Packaging | The engine ceiling is 17,667 physical Python lines. The installer contract covers 18 slash commands and 12 mapped scripts. The packager includes repository Markdown by default, including this planning file if present in the release input. |

Read the [controller contract](../skills/kh/SKILL.md), [project tracking](../skills/kh/references/project-tracking.md), [owner intent](../skills/kh/references/intent.md), [model choice](../skills/kh/references/model-choice.md), and [verification principles](../skills/kh/references/verification-principles.md) before implementation. In the table above, visualizer paths are relative to `.claude/skills/kh/apps/visualizer/` and Python paths to `.claude/skills/kh/templates/adws/`.

LoopForge supplies product references for an attempt-specific approval screen and for linking recovery to a run. KH uses its existing sequential task and receipt contracts. [LoopForge approval screen code](https://github.com/Tencent/LoopForge/blob/217df4196fc822714f868e1eda33fadc52a4a0f6/web/app/approvals.js) · [LoopForge retry operation](https://github.com/Tencent/LoopForge/blob/217df4196fc822714f868e1eda33fadc52a4a0f6/internal/engine/run_ops.go#L437)

## Owner workflow

1. Open a stopped task. Show its stop reason, selected run, attempt time, and recorded phase. Open the most recent stopped attempt by default. Give previous attempts stable links.
2. Inspect the recorded check or review finding. Group the published unified diff by file and show added and removed lines with line numbers. If the attempt stopped before creating a diff, show the check output and say that this attempt has no published diff.
3. Write a task-level reply or attach one to a specific line. Show the target next to the draft. The owner can save the reply and start the next attempt as separate actions on the same page.
4. Show the mode and effective configured role choices in the start preview. The first release lets the owner explicitly choose the saved setup for this attempt. Use the normal preflight, including context readiness and permission checks. When the owner wants different role choices, link to the existing per-run CLI flow.
5. Show the resulting run beside the original attempt. Display the builder handoff, new change, and recorded check and review outcomes. Show project completion when the controller's ledger records it.

| Recorded situation | Action on the page |
| --- | --- |
| Code or test correction within the approved task | **Request changes** with a task-level or line-level reply. |
| Environment or provider blocker | Show the diagnostic. Offer **Retry task** after the owner resolves it and explicitly requests a new attempt. |
| Sensitive change held by owner policy | Show the published change and category. Link to the current intent and the re-acceptance instructions. |
| Missing owner evidence decision | Show the draft and the applicable decision path. Offer a command preview for guarded review of eligible checked work. |
| Active repair, completed task, superseded attempt, or unknown stop | Show recorded facts and history. Enable actions according to the authoritative current state. |

## Review and action contract

**Attempt identity.** A review response binds evidence to a task ID, run ID, and phase or correction round when recorded. A stable artifact identifier resolves through the selected run's published handoff. Serve a bounded set of text artifacts with existing path and file checks. Diff paths and text come from controller evidence. Use text rendering, a size limit, and an explicit unavailable state for legacy, binary, absent, or truncated evidence. A fresh projection read establishes when the UI fetched state. The repository lease and ledger establish whether an action is available.

**Feedback identity.** A versioned local record contains a request ID, task ID, stopped run ID, task definition identity, expected blocker state, action, text, and creation time. A line reply includes the published patch digest, path, side, line number, and line-text digest. A task-level reply is anchored to the attempt. When it refers to a particular displayed change, bind that artifact too. The project root, executable, run ID, and configuration source for launch are resolved locally. Feedback lives in controller-owned local state and enters the builder handoff as bounded owner guidance. Keep an immutable reference and digest in the attempt history.

**Authority and freshness.** Browser start is a new local capability. Enable it from the owner-controlled launcher, with loopback Host checks, exact Origin validation, a per-launch capability, bounded JSON, and fixed action types. Use the same project root for reads and writes. Pass validated IDs to the existing guarded script through a fixed argument vector and resolved working directory. Resolve model choices from the selected project's existing configuration. Acquire the repository lease before binding intent, changing ledger state, or dispatching a role. Recheck the latest blocked run, current definition, dependencies, intent, and artifact identity. A line reply requires ready context, a reusable approved plan, and matching kept work. Check eligibility before dispatch and confirm the staged bytes match the target before the builder sees the reply. Surface a stale-reference conflict and preserve the draft if the binding fails. Reuse the controller's plan and resume validation.

**Dispatch and result.** Reserve a fresh run ID for each start request. The same request ID returns the same known state and run ID. Record the reservation durably before launching and correlate the guarded attempt to it when the existing ledger records the run. Treat a crash between reservation and launch confirmation as an unknown launch outcome. Reconcile it using the controller before the owner can explicitly retry. A concurrent CLI run or another browser request competes for the same lease. Saving a draft, refreshing the page, and recovering the UI start zero workers. The controller receipt establishes the result. Browser state can show saved, starting, running, stopped, accepted, and unknown based on the request and controller records.

**First-release boundary.** Browser actions cover a new attempt of one tracked task and scoped correction feedback. Intent edits, task revisions, sensitive approvals, and historical evidence exceptions use their existing owner paths. Live roles finish or stop under the current controller contract. A scope-changing request enters the accepted-intent and task-revision path. The normal plan identity, freshness checks, attempt budgets, quality commands, and independent review apply to every retry.

## Sequential implementation tickets

### RV-01. Add the read-only task review page

- **Change.** Add a review detail component and route from `ProjectOverview.vue`. Use `App.vue`, `src/lib/router.ts`, `src/lib/api.ts`, and `shared/types.ts` for wiring. Keep existing run deep links usable. Add a read endpoint in `server/index.ts` and a focused server module for assembling run evidence.
- **Implementation.** Resolve task membership and attempt identity from the recorded project and run data. Reuse the existing safe path helpers and trace API. Read the selected run's published artifact map, check result, and review records. Serve the exact available diff as text, with size limits. For old data without enough provenance, show the stop reason and a link to the existing trace. Use the existing report or ledger contract if the current project projection lacks history. Keep the read model derived.
- **Acceptance.** From the project page, a stopped task's first relevant evidence opens in two clicks or fewer. A failed-check run with no patch shows its own check output and an unavailable diff. A held run shows its own patch. A prior attempt stays linked to its original evidence. Keyboard navigation reaches file selection, code lines, and the return link.
- **Validate.** `python3 -B -m unittest tests.test_visualizer_api tests.test_dashboard_projection`. Add HTTP regressions for cross-task artifact access, missing phases, stale projections, symlinks, and oversized artifacts. Run the visualizer checks below.

### RV-02. Add the guarded feedback handoff

- **Depends on.** RV-01's stable evidence identifiers.
- **Change.** Extend `adw_start.py` to accept a validated review request by ID through the existing single-task entry point. Use the existing lease and task-event code in `adw_modules/session.py` and `adw_modules/project.py`. Reuse `evidence_packet.py` for a controller-owned feedback artifact and the existing builder handoff for scoped guidance. Keep generic `--evidence` semantics for original evidence. A feedback request has a separate typed path.
- **Implementation.** Validate the task and request under the repository lease before intent binding or dispatch. Compare the selected stopped run and task definition with the ledger. For a line reply, check the artifact, current owner baseline, and kept-work eligibility using existing fingerprints and the normal staged scope. If context or plan reuse fails, offer a task-level draft for explicit owner review. Recheck the anchor against the approved plan and staged candidate before builder dispatch. Write the request ID and digest to the new attempt so the review page can follow it. For task-level feedback, use the existing guarded builder flow and report any plan reuse or replan.
- **Acceptance.** A valid reply reaches the builder as scoped guidance tied to a new run. A stale task, replaced patch, changed kept candidate, ineligible line, busy repository, or duplicate start produces no additional builder call. A scope change requires the existing revised intent and plan path. A failed check or blocked independent review prevents promotion. Runs without feedback follow the existing entry path.
- **Validate.** Introduce and run `python3 -B -m unittest tests.test_task_review_feedback`. Run `python3 -B -m unittest tests.test_resume_work tests.test_owner_review tests.test_evidence_delivery_repair tests.test_run_next tests.test_package.PackageTests.test_engine_stays_within_owner_approved_line_ceiling`. Verify the before and after line counts within 17,667 in the same change. Cover any needed consolidation with focused contract tests.

### RV-03. Save replies and start a guarded attempt from the visualizer

- **Depends on.** RV-02's validated entry point.
- **Change.** Add an optional local action configuration to the Bun server and UI. Add a compose panel with **Save reply** and **Request changes**. Add status polling for the request and its linked run.
- **Implementation.** Save the draft under `.kh/review-requests/` with atomic creation. Keep UI request state separate from task acceptance data. Use two bounded operations, save and start. Reserve the run ID atomically when the owner starts. Launch the existing guarded script with fixed arguments and validated IDs. Feed routing choices from the local project, with an explicit saved-setup confirmation. Capture the child outcome with the native handle and reconcile it against the recorded run. Existing route guards apply and mutation routes require the local action capability. [Bun child process documentation](https://bun.com/docs/runtime/child-process#spawn-a-process-bun-spawn) · [Fetch Origin header](https://fetch.spec.whatwg.org/#origin-header)
- **Acceptance.** Saving or reloading a reply starts zero agents. Repeated start requests produce one guarded attempt. A stale tab preserves the draft and points to current evidence. A foreign origin or disabled action configuration starts zero agents. Closing the tab and returning shows the same request and run. A crash at launch shows its known or unknown state with a recovery path.
- **Validate.** Extend `tests.test_visualizer_api` with an instrumented launcher and run `python3 -B -m unittest tests.test_visualizer_api tests.test_task_review_feedback tests.test_controller_state_contract`. Exercise a real browser against a temporary project with deterministic providers. Assert the reply, one new attempt, check failure or success, and the controller's recorded result.

### RV-04. Route held decisions and environment blockers

- **Depends on.** RV-03's action controls.
- **Change.** Present environment retry, hold guidance, and historical evidence decision guidance in the review detail page. Use recorded stop categories and the run's decision drafts. Keep unknown and legacy reasons visible as recorded.
- **Implementation.** An environment retry uses the same single-task admission and preview. A sensitive hold opens the existing intent and displays the relevant category. An evidence decision can display the existing draft and exact invocation IDs. Offer `--review-existing` only for its existing eligible checked-candidate path, with its potential to promote stated before use. Use `--advisory` for optional review-only inspection with no promotion. Keep formal decision entry through the existing owner workflow.
- **Acceptance.** A regular reply cannot clear either owner-decision blocker. A re-accepted intent or applicable recorded disposition flows through its existing verification route. A generic retry stays single-task. A missing draft shows the recorded blocker with a trace link.
- **Validate.** `python3 -B -m unittest tests.test_owner_review tests.test_evidence_delivery_repair tests.test_task_review_feedback tests.test_visualizer_api`.

### RV-05. Demonstrate and release the review workflow

- **Depends on.** RV-04.
- **Change.** Create a repeatable demo using the existing deterministic test helpers and a temporary project. Document how to open the review view, use the optional action mode, and return to the existing CLI path. Update the user guide and verification record with measured results.
- **Demonstrate.** Start with a stopped task and an actual check failure. Send a correction and verify the literal fixed behavior. Open a held task and inspect its published diff. Reuse a stale tab after the candidate changes and verify the launch is refused. Include a legacy no-diff run.
- **Measure.** Evidence is reachable in at most two clicks. The owner can finish the correction round trip in the same browser tab. Each action produces the expected number of guarded attempts. Record elapsed time and trace evidence for the demo. Leave production intervention counts unknown until a project supplies them.
- **Validate.** Run the full source and extracted-archive gates below. Check both a new installation and an upgrade with the visualizer installed. The older installation continues to show project and run data. The action controls require a compatible installed controller and explicit activation.

## Follow-up tickets

### RV-06. Add versioned task templates

- **Change.** Add bug fix, interface feature, and maintenance templates using `execution.task_kind`, the existing intent format, and the verification recipe contract. Generate a draft request, required evidence, and prompts to select real project commands. Keep the templates as data attached to the current guarded workflow.
- **Acceptance.** The generated draft validates against the intent and plan formats. The owner can inspect and accept it through the existing path. Template version appears in the generation output. Existing accepted intents and templates from prior installed versions remain readable. A missing test command is shown before work starts.
- **Validate.** `python3 -B -m unittest tests.test_intent tests.test_acceptance_recipes tests.test_plan_review_cli tests.test_install`. Add a fixture that generates, accepts, and dry-runs each draft against configured project commands.

### RV-07. Export a task delivery packet

- **Change.** Produce a local Markdown summary with the task request, run ID, changed files, controller patch, reported checks, independent review, acceptance receipt, and selected test artifacts. Start with a local preview and export from the review page. Use the receipt validation and published evidence paths as the source. Keep output bounded. The owner selects any raw logs for inclusion.
- **Acceptance.** An accepted export is keyed to its receipt and verified source digest. An incomplete run is labeled as such. Missing or modified evidence is explicit. Existing receipts remain readable. A later PR integration can generate a draft description from the packet and submit it only after an explicit owner action.
- **Validate.** `python3 -B -m unittest tests.test_evidence tests.test_evidence_delivery_repair tests.test_visualizer_api tests.test_package`. Add export cases for an accepted run, a stopped run, changed evidence, and a file outside the published artifact set.

## Validation and rollout

Run the focused tests for each ticket first. Use the pinned development dependencies. Run these commands from the distribution root for the release candidate.

```bash
python3 -m pip install -r requirements-dev.txt
python3 -B -m unittest discover -s tests
python3 -B scripts/run_evals.py
python3 scripts/package.py --output /tmp/kh-software-factory.zip
```

Run the following from `.claude/skills/kh/apps/visualizer/`.

```bash
bun install --frozen-lockfile
bun run typecheck
bun run lint
bun run build
```

| Release scenario | Required observation |
| --- | --- |
| Stopped run with no diff | Actual check failure and explicit missing diff. |
| Builder correction | The marker reply reaches the intended builder call. The exact changed behavior is checked and independently reviewed before the ledger completes. |
| Stale or replaced target | Freshness is checked by the controller. The UI retains the reply. The stale request dispatches zero agents. |
| Duplicate or concurrent start | One run per request. The repository has one active writer. |
| Lost browser response or server restart | A known run is reattached. An uncertain launch is surfaced for reconciliation. |
| Owner hold | The selected attempt and owner decision path are visible. Promotion requires the existing owner authorization and controller gates. |
| Missing or older visualizer data | Existing monitoring works. Unsupported actions are shown as unavailable. |
| Action mode disabled | The visualizer starts in its existing monitoring mode. The CLI is available. |
| Source and archive | Line ceiling, command mapping, package integrity, full suites, and installer smoke pass. |

Begin with RV-01. Build RV-02 and RV-03 as the first end-to-end release increment. Use a temporary test project until the deterministic release gates pass. Then run the owner-facing cases against one opted-in real project and record which cases used live providers. Treat the line ceiling, exact evidence identity, and uncertain launch handling as the primary implementation risks.

This plan assumes a single local owner, a single reply per new attempt in the first release, and explicit use of the saved role setup from the browser. It uses the current controller design to resolve those choices. The first delivery milestone has no blocking product decision.
