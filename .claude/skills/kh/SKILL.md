---
name: kh
description: Run and maintain the KH cross-project workflow controller, including natural-language requests to execute a recorded project fully or continuously, configured coding agents, deterministic checks and trace. Use for KH setup, slash commands, workflow execution or workflow configuration.
---

# KH Software Factory controller

Use one controller for task order, retries, evidence and acceptance.
delegate-skills is a transport adapter; task-observer is an advisory recorder.

## Routing

- Setup: [cookbooks/install.md](cookbooks/install.md).
- Execute or maintain workflows: [cookbooks/run_adw.md](cookbooks/run_adw.md).
- Models, tools and checks: [references/config.md](references/config.md).
- Owner model choice and changes: [references/model-choice.md](references/model-choice.md).
- Project tasks and milestones: [references/project-tracking.md](references/project-tracking.md).
- Trace inspection: [references/observability.md](references/observability.md).
- Output contracts: [references/handoff.md](references/handoff.md).
- Builder/reviewer judgement rules: [references/verification-principles.md](references/verification-principles.md).
- Documentation prose: [references/prose-standard.md](references/prose-standard.md).

Without a task, show available workflows. When a task is already supplied,
route directly. Preserve the user's project scope and existing authorization.

For a recorded project plan, use `/kh-project init --plan project-plan.json`.
Run the recorded plan with `/kh-project run-continuous`, or name one task with `/kh-start --task TASK_ID`.
When the owner says in natural language that the whole project should be executed
to the end (for example, `execute the whole project`, `execute the project to completion`,
`continue the project`, or the equivalent in another language), treat that as direct
authorization to prepare and execute the project. Review the overall plan and
its ticket decomposition, then use `adws/adw_project.py run-continuous`.
Execution is sequential; do not propose or launch a parallel strategy.
Do not ask for a task ID,
task count, or repeat the internal command when the ledger and required setup are
ready. If setup is missing, report the concrete prerequisite instead of inventing it.

The current conversation model is the conversational controller: it translates
owner intent, starts and monitors the deterministic controller, and relays every
`KH_TASK_COMPLETED` event immediately before allowing the next task to proceed.
It is not the acceptance authority. The Python controller, SQLite ledger,
configured checks, independent review, promotion and receipt decide completion.
Planner, builder and reviewer models remain the owner's role choices. Before
a live run or batch starts, ask the owner once which model and effort each role
uses, and pass the answers as `--use` for that run only
([references/model-choice.md](references/model-choice.md)). Never ask during
the cycle on the controller's own initiative. Running roles keep the choices
resolved at startup. Permanent setup waits until the active writer ends; a
requested different choice can be recorded for a later run. Never create
mid-run model-switch state or change the active run's scope, tools, deadlines
or counters. When no one can answer, use the recorded setup and say so.

Keep `run-continuous` attached to the active conversation.
For `run-continuous`, when the owner says `pause after the current task` (or equivalent),
request `pause-after-current`; the active task may finish, its result is reported,
and no later task is dispatched. `continue the project` starts a new continuous pass from
the next pending ready task. A task failure stops automatically and is reported;
never skip it or retry a blocked task without resolving the blocker. The controller
documents each agent handoff and only counts reviewed, checked and promoted work
as done. Use `/kh-project status`, `controller-status`, and `next` to inspect
progress without starting work. Historical parallel journals and execution
choices are refused before dispatch. Preserve them and use the prior release
for reconciliation; never reinterpret them as sequential approval.

For a small isolated task, use `/kh-simple-sdlc`, the alias for `/kh-start --mode light`: reviewed planning, build, real checks and independent review, with one plan repair and one shared code repair at most. Use `/kh-models --doctor` for local relay diagnostics.

For a new bounded task, use `/kh-start` with the supporting
[model-choice reference](references/model-choice.md) and `kh-scope-guard` policies. `/kh-models` lists configured
choices without provider calls; `/kh-scope` reads the saved task boundary.
At project start, `/kh-setup-models` records the owner's per-role model and
effort choices (validated, dry-run by default) for guarded startup to resolve.
These policies advise this controller and cannot disable its permissions or
acceptance checks. Explicit standalone workflows keep their own documented scope.

Guarded startup first prepares missing project context with a read-only architect
and independently reviewed documentation publication. `/kh-bootstrap --refresh`
updates context deliberately; `CLAUDE.md` and `AGENTS.md` route to domain rules.
With closing documentation enabled, record an explicit owner choice for the
`documenter` role too. The approved plan declares documentation impact and exact
targets within owner scope. The documenter proposes content with source read-only;
the controller validates and stages it before checks and independent code review.
Active guides and historical change records remain distinct. Only reviewed,
promoted documentation enters the derived context index before completion. Legacy
configs without this setting report the stage as unconfigured.
Keep current guides and their `conditional_docs.md` catalog in `docs/agent-guide/`;
new historical records belong in `app_docs/`. For guides with reviewed `read_when`
conditions, planning records a complete reasoned read/skip selection. Independent
review checks it against the task and receives selected contents. Mandatory
instructions remain effective. Conditional plans need explicit approval without
substantive findings before building. Changing conditions requires the exact
catalog target in the existing owner scope; the controller renders it before
checks/review. Selection changes no tool or permission grants.
Project batches request the same reviewed refresh for stale context before each
task, under their existing lock and role choices. `/kh-start --refresh-context`
does this for one task. A rejected context review or an unresolved owner decision
stops execution. Review-only candidates never prepare or refresh context.
Use `/kh-project amend` or `split` to revise pending or blocked tasks while the
project is idle, with accepted matching intents and an explicit reason. These
commands retain history and candidates; split preserves scope, checks and total
weight. They cannot revise accepted tasks or mark tasks complete.
Full task execution may spend predeclared recovery rounds to continue safely
interrupted builders or progressing repair windows. Missing plan paths require
a new independently approved plan version inside the original owner's explicit
scope. Failed attempts, old approvals and findings remain evidence; deadline
and send counters never reset, and spend is informational. This does not permit
restarting an unresolved owner or infrastructure blocker or expanding task scope.
`/kh-plan-review` runs only bounded planning/review/correction. Every revised
plan requires exact-version approval before implementation. Planning approval
never completes a project task.

Guarded startup holds the complete candidate task in staging until real checks
and an independent read-only review pass. Review covers original requirements,
non-goals and every changed source file. Rejected tasks do not import source;
Checks that mutate source fail and cannot authorize promotion; disposable
generated directories are handled separately. Candidate evidence remains available with its actual outcome. Bounded repairs
rerun checks and review against changed source; no stale-result cache is used.

## Execution boundary

Each agent phase uses an isolated working copy. The controller captures real
content, modes, symlinks and Git state; checks permissions after every send;
validates typed output and evidence; then imports only permitted changes if the
owner workspace still matches its baseline. Failed phases are discarded.
Earlier promoted phases remain for review: failure is not whole-run rollback.

This is a promotion boundary, not an OS/network sandbox. Native provider
permissions still apply. Never bypass denial. Control definitions and other
phases' evidence remain controller-owned. Use the supplied per-phase output path.
Native sessions are scoped to one phase; retries within that phase retain context.
Joined KH sessions preserve controller evidence and requirements, not implicit
conversation sharing across providers or stale absolute workspace paths.

Roles may declare `required_capabilities`. Check known tool/sandbox restrictions
and exact native-command grants before dispatch. Legacy Swift/database names
require native shell configuration; access remains unverified until actual tool
receipts. Role `capability_probes` and `review.panel` are retired compatibility
settings with no effect. Each review round retains one independent combined
reviewer. Source-only Swift edits can use file tools while the controller runs
quality commands. Never elevate permissions or silently replace a model after a
failed compatibility check. Infrastructure worker restrictions remain unchanged.

## Acceptance

- Missing real quality commands produce NOT_CONFIGURED before test-workflow dispatch.
- Require process exit zero, no reported error and terminal protocol completion.
- Plan/scout/review/document roles require real nonempty evidence files.
- Build claims must match the captured changed-file set, including deletions.
- Review findings must cover every controller requirement with concrete evidence.
- Retest every applied repair. Finish with Run.finish(accepted=...).
- Runs never wait for the owner mid-task. When the verified delta touches an
  `execution.owner_review` category, the reviewer must justify it ("Owner
  attention" requirement), the task promotes and closes itself, and the flag is
  listed in the receipt and task report for the owner afterwards. Only
  `mode: hold`, which the owner sets explicitly, stops with
  OWNER_REVIEW_REQUIRED. Never add approvals to an intent or write its
  `Accepted digest` yourself.
- Workflows do not create automatic commits; the auto_commit compatibility
  key is accepted without effect.
- Do not infer authorization to push, deploy or alter external services.

Optional observations are recorded locally by the controller after promotion.
Observer failures remain advisory. Shared knowledge and skill updates require
explicit sanitization, review, evidence and versioned adoption.
