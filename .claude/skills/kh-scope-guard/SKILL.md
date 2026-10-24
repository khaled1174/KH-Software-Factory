---
name: kh-scope-guard
description: Define and preserve an KH task’s original request, deliverables, non-goals, allowed paths and required verification so implementation stays within the authorized objective.
---

# KH Scope Guard

Support the existing KH controller with a bounded task contract. Keep the
original request and explicit constraints distinct from the planner's proposed
steps. Planner output can refine execution; it cannot silently replace the
user's requirements. The controller owns acceptance and file promotion.

## Define a useful boundary

Capture the original request, deliverables, non-goals, constraints, allowed
paths, required checks and whether review is required. Reuse explicit scope
already established in the conversation. Ask only about a material unresolved
product, security or authorization ambiguity; do not require routine approval
of each phase. Use [references/scope.md](references/scope.md) for the JSON input
and examples, then start the task through `/kh-start --scope-file PATH`.

Keep deliverables observable. An allowed file path does not authorize unrelated
features within that file. An incidental helper or test needed for the requested
behavior can remain in scope when it respects explicit constraints and paths;
record the reason instead of treating every omitted plan step as new work.
A change outside allowed paths needs an explicit revised scope, even when its
intent is useful. Record unrelated improvements for later through task-observer
when present; do not execute them as part of the current task.

## Verify the current delta

Use project-configured checks that apply to the requested behavior. The guarded
startup requires configured checks and independent review; this skill cannot
mark a required check optional just to continue. A missing required result is
unknown, a nonzero test exit is failed, and an agent's success claim is not proof.
Optional report formatting and repetitive progress narration are not gates.

Combine specification and quality review for one coherent code delta. Give the
reviewer the original contract and actual changes in a separate session. If
material fixes change that delta, rerun affected checks and perform focused
review of the changed behavior; one review is not an absolute lifetime cap.
Respect the controller's bounded repair attempts and report unresolved defects
when that budget is exhausted. Do not start unlimited correction loops.

Do not import the uploaded legacy gate checker, sealed ledgers or its cached
checkbox results. Rerun required checks for the accepted source state; do not
claim cached success after source, commands, dependencies or relevant execution
environment change. Do not weaken KH's transport, evidence, permission or
concurrency checks in the name of avoiding paperwork.

## Completion and changes of direction

Report completed deliverables, changed behavior, actual verification and review
outcomes, deferred findings and any external effects performed. Scope semantics
still require review: machine-enforced path checks alone do not prove relevance.
When a later user instruction legitimately changes the objective, record the
revised bounded scope explicitly before dispatching the new work. Preserve
existing owner changes and do not infer permission to push, publish or deploy.

This adaptation retains the applicable notice in [LICENSE](LICENSE). Its scope
contract is implemented by KH; it does not activate a separate gate engine.
