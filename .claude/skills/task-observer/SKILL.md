---
name: task-observer
description: Controller-only capture and review of evidenced lessons. Use at task delivery or when explicitly reviewing observations; workers return compact observations to their controller.
disable-model-invocation: true
---

# Task observer — KH integration edition

Operate only through the primary controller. Capture a useful correction or repeated
failure at task delivery; do not load this protocol in every worker or before every
tool call. Observation failure never changes a task verdict or blocks unrelated work.
The KH task ledger remains authoritative. Observations do not advance its states,
change its acceptance criteria, or grant additional permissions.

1. Record issue, suggested improvement, reusable principle, skill, and controller-supplied
   run/phase/source revision through `scripts/observer_store.py`. Keep evidence and raw
   details inside the originating project. Exact duplicate content shares an observation
   ID while preserving separate provenance for every occurrence.
2. Inspect only relevant records for the current project. Read `references/observation-log.md`
   for the API; read `references/signals.md` when deciding whether an event is useful.
3. Offer an optional review when the deterministic `review-status` command says it is due.
   Do not install hooks or schedules automatically.
4. An authorized skill change needs independent review and passing repository contract
   evals (`scripts/run_evals.py`) on the changed tree before installation. A failing
   suite blocks installation. Editing and installation are separate authorized
   operations outside this helper; it has no proposal or installation registry.
5. Promote a lesson across projects only after explicitly reviewing separately authored,
   sanitized text. Never export issue text, evidence, client names, or source paths by default.

A permission denial ends that action. Report it to the controller and follow an existing
authorized approval path if available. Never switch tools, paths, identities, or settings
to get a denied action through. Transient technical failures may be retried within the
same authorization, with a bounded controller retry; this helper never retries writes.

Use Python filesystem/SQLite operations rather than ad-hoc shell loops. Missing or
unreadable storage is an error, not an empty-log signal. No model training, background
daemon, network request, installation, automatic active edit, or self-acceptance occurs.

Read `references/environments.md` for setup and path handling and
`references/starter-principles.md` for optional review guidance.

This is a modified adaptation of Eoghan Henn / rebelytics task-observer, pinned at
2967fa5f2f16336677d216fe83d9832a52aadc00. See ATTRIBUTION.md and LICENSE.txt (CC BY 4.0).
