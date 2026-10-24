# Project context and documentation preparation

`/kh-bootstrap` prepares a reviewed documentation map grounded in the current
repository. `/kh-start` prepares missing context before task planning, under the
same controller. Valid context is reused. `--dry-run` reports readiness without
model dispatch or writes; `--refresh` explicitly reassesses existing context.
`/kh-start --refresh-context` refreshes stale context before planning. Project
`run-continuous` request that reviewed refresh automatically under
the existing lock and role choices. A rejected review or an unresolved owner
decision stops the task. Review-only candidates keep their original context;
`--refresh-context` cannot accompany `--review-existing`.
Both review-only modes require ready context; a missing map is refused without
creating navigation documents or dispatching an architect.

## Responsibilities and file layout

The owner-selected `project_architect` inspects the project and proposes document
contents. The separate `plan_reviewer` reviews the exact proposal. The controller
validates and publishes an approved, limited documentation set. Neither role has
permission to edit source directly or grant itself access to protected files.

Keep `CLAUDE.md` and `AGENTS.md` concise routing entry points. Both should refer to
canonical documents rather than duplicate architecture rules. Suggested paths:

| Document | Purpose |
|---|---|
| `docs/agent-guide/project-map.md` | Existing directories and task-to-document routing |
| `docs/agent-guide/architecture.md` | Boundaries and established/proposed decisions |
| `docs/agent-guide/rules.md` | Durable rules, evidence and enforcement |
| `docs/agent-guide/testing.md` | Acceptance evidence and test guidance |
| `docs/agent-guide/commands.md` | Controller-derived configured checks and package.json scripts |
| `docs/agent-guide/user-journeys.md` | Important flows across modules |
| `docs/agent-guide/conditional_docs.md` | Reviewed conditions for reading feature guides |
| Domain documents when relevant | Frontend, backend or persistence conventions |

Use two project documentation directories. `docs/agent-guide/` holds current
guidance and its conditional catalog. `app_docs/` holds new historical records
of delivered changes. Root instruction files remain routing entry points.

Reuse existing canonical documents. Do not create empty domain files for stacks
that are absent, move source directories, or describe a proposed feature as
implemented. Missing material architectural decisions remain explicit blockers.
Root router size is a readability concern, never permission to truncate owner rules.

The context index `.kh/project-context.json` records managed paths and digests.
The controller attaches applicable document contents and provenance to worker
briefs; a Markdown pointer alone is insufficient. Required documents cannot be
silently omitted to fit a context budget. Root instructions are always loaded; nested instructions retain their directory
applicability. Scoped tasks load relevant domain documents, while an unscoped
planning pass receives explicit applicability metadata and a current observed map.
Required serialized context is bounded to 128 KiB. Generated documents cannot override higher-authority rules.

## Preservation, refresh and recovery

Publication is controller-owned and limited to validated documentation paths.
Traversal, symlinks, broken required references and stale owner baselines block
publication. Existing owner edits must be preserved or surfaced for resolution;
refresh is not permission to erase instructions. Unchanged controller-generated
documents may receive independently reviewed updates. An owner-edited document
is retained verbatim and treated as owner-preserved on subsequent refreshes. Protected runtime, secrets and
source files do not become architect-writable.

Publication uses a recoverable journal and backups. Several file replacements
are not one filesystem transaction: readiness is recorded only after publication
finishes. Interrupted preparation must recover before a builder can proceed.
Source structure, manifests, guard scripts, and linked targets are checked for
drift. Disposable generated output is excluded while tracked source remains visible.
Outside a project batch, use `/kh-bootstrap --refresh` or `/kh-start --refresh-context`
for stale context and inspect the exact blocker
if it fails. Never delete the journal or ready marker to force execution.

`max_bootstrap_repairs` defaults to 2 (range 0..5), separately from planning and
code corrections. Context readiness does not mean required quality tools exist.
Suggested lints/hooks/test adapters are guard tasks for normal scoped execution;
bootstrap does not run arbitrary installation commands found in documents.
No commit, push, merge, deployment or visual baseline acceptance is implied.

## Document lifecycle and selection receipts

Reviewed context documents carry `purpose` (`guide`, `change_record`,
`run_evidence`, `external_reference`), `status` (`active`, `historical`,
`superseded`), optional topic identifiers, `supersedes` paths and an optional
recorded `source_revision`. Old indexes without these fields retain active-guide
semantics. A recorded revision is provenance, not proof of semantic freshness.
Only active guides are eligible for task context. Historical documents remain
on disk. A replacement must name an indexed superseded document; cycles fail.
Root and nested CLAUDE/AGENTS instructions must remain active guides.

On refresh or a closing-document proposal, omitted metadata for an indexed path
inherits its current lifecycle and applicability. Validation resolves these
values before review; an explicit transition needs review of the changed values.
The refresh baseline includes existing index metadata and binds the prior index
bytes, so content-only updates cannot silently reactivate history.

The packet records every selected and omitted path with its reason and includes
that receipt in the 128 KiB limit. Directory applicability remains mandatory.
Topic hints may add a relevant guide, including a guide in another source area;
they cannot bring foreign directory instructions into scope or remove required
rules. An unscoped planning pass retains all applicable unconditional active guides. Topic
hints are advisory matching, not a router or a source of permissions.

## Conditional feature guides

An active feature guide can declare `read_when` in its reviewed context metadata:

```json
{
  "path": "docs/agent-guide/csv-export.md",
  "content": "Current CSV behavior and verification instructions.",
  "applies_to": ["src/**"],
  "read_when": [
    {"id": "csv-change", "condition": "Read only when changing CSV export."},
    {"id": "csv-bug", "condition": "Read only when fixing empty CSV downloads."}
  ]
}
```

Condition IDs are unique per guide, lowercase, and at most 48 characters. Each
guide has at most 16 conditions. Conditions are nonblank single lines of at most
1,000 characters. Any matching condition requires reading the guide. This uses
the planner's task judgement; there is no keyword or path matcher for conditions.
`CLAUDE.md`, `AGENTS.md` and the catalog itself cannot be conditional. Other
mandatory owner rules must also remain effective and are checked in review.

The planner initially receives the catalog and conditional metadata without
feature-guide bodies. Broad source scope, topic overlap and an unscoped task do
not load those bodies. It inspects matching guides with existing read tools and
returns `context_selection`, covering every catalog path exactly once:

```json
{
  "path": "docs/agent-guide/csv-export.md",
  "document_digest": "<digest supplied in conditional_documents>",
  "decision": "read",
  "condition_ids": ["csv-bug"],
  "reason": "This task repairs downloads that contain no CSV rows."
}
```

Use `skip` with empty `condition_ids` and a task-specific reason for unmatched
guides. Report `unresolved` if matching or inspection cannot be established; it
cannot authorize building. Missing, duplicate, unknown or stale decisions fail
before review. Selected bodies must match their frozen digest and fit the same
128 KiB context budget. Candidate revisions read the original task-baseline
guide, even when the candidate updates that guide.

Independent plan review receives the selected contents and verifies every
decision and the plan's use of the guidance. Conditional plans require explicit
approval without unresolved substantive findings. These cannot become final
binding build notes. The controller records `planning/context-selection.json`,
updates the understanding receipt and delivers the approved selection to later
roles. Decisions belong to the plan digest; the original pending catalog belongs
to the context digest. Reuse and revisions validate these same identities.

Selection receipts prove delivery and recorded reasons. They do not prove
comprehension, semantic matching accuracy or operating-system read isolation.
They add no tools or permissions. Legacy guides without `read_when` keep the
existing path/topic behavior and serialized identity. Omitted conditions inherit
on content-only refresh; an explicit `[]` removes them through review.

## Controller-derived bootstrap documents

Active scoped guides also bind the content hashes of observed source files that
match their resolved applies_to paths. These controller-owned source_digests live
in the index, not the ordinary context packet. Hashing refuses links, pipes,
oversize files and files replaced while reading; it does not require Git. General
navigation with omitted or universal applicability keeps file-name freshness.
Existing structure, manifest and document checks still apply to every context.
A changed covered source names both guide and source in the stale finding. An old
scoped guide without a baseline is stale. Merely retaining it cannot approve a
new source baseline: bootstrap must explicitly propose it for independent review.

The existing documenter receives affected scoped guides. It must update each
within the approved targets or return a digest-bound confirmed_documents entry
after checking that its unchanged text remains accurate. Confirmations cannot
write a file or change its metadata. Only reviewed updates and confirmations
receive new source bindings; disabled or not_needed documentation leaves the old
binding stale. Existing preparation refreshes it before the next consumption.
Saved candidates retain review-time source_proofs for updates and confirmations.
KH checks those proofs before rebinding an imported receipt, at index projection,
and after promotion. Source drift refuses reuse through the existing scoped
documenter fallback; it does not grant a broader builder or documentation scope.

The architect proposes guides and their read_when metadata, not commands.md or
conditional_docs.md. A model-supplied controller document is discarded with a
warning before review; it does not spend a bootstrap repair. The independent
reviewer approves the exact remaining guide proposal and its resolved conditions.
Only after that approval does the controller derive the conditional catalog
with render_catalog. Publication rechecks the proposal identity, owner baseline,
index and exact projected packet budget. Changed conditions invalidate approval.
An existing catalog becomes consistently empty when the last condition is removed.
No normalization receipt or normalization_digest is needed for bootstrap.

The controller also derives commands.md from the selected quality_checks,
required_quality_checks and scripts maps in observed package.json files. Other
manifest formats are not parsed into command declarations here. Project-local
absolute paths use <project> so the guide has identical bytes in isolated copies.
A missing script
declaration does not establish that no command exists. These facts neither prove
execution nor grant permissions. Warm live preparation synchronizes this managed
guide before task or shared-context binding, without another model call. It
preserves owner edits and unrelated stale document, manifest and structure
baselines; unchanged facts produce no guide or index writes. Inspection, dry-run,
saved-candidate review and binding checks remain read-only.

## Bootstrap revision continuity

Each repair receives the complete previous proposal with resolved metadata, cumulative
finding identities and prior validation/review records in `revision_context`.
The architect edits that draft, preserving corrections already verified against
source. A later finding about another section does not discard earlier
corrections. Original owner instructions and the observed baseline remain the
authority; prior role output cannot create new owner rules.

The controller persists `context-revision-<attempt>.json` before dispatch and
`context-attempt-<attempt>.json` with the proposal, validation and final review.
Historical proposal bodies remain in their attempt records. The brief includes
the latest complete body and all earlier validation/review summaries, without
duplicating every older body. Identical finding text and kind have the same
controller identity across rounds, with each occurrence retained. History belongs
to the current bootstrap run and its existing bounded repair loop.

A revised `ContextReviewOutput` binds `revision_context_digest` and returns one
`revision_checks` entry per historical finding. Each entry copies the finding ID,
states `resolved`, `unresolved` or `superseded`, and cites correction evidence.
An earlier mistaken prescription can be superseded only with an independent
source-based explanation. Earlier resolved findings are rechecked for regression.
Missing, duplicate, unknown or stale checks cannot approve publication. The native
gate uses the phase's existing correction allowance; no allowance is increased.
The controller repeats validation before accepting the review and retains the
revision-context digest in the approval receipt.

Digests describe canonical controller values before ordinary workspace path
mapping, as with catalog provenance. They establish delivery and identity,
not semantic correctness. The independent reviewer still checks actual source
and guide content. Publication still requires the exact current proposal,
approval with no findings or policy conflicts, preserved owner/source state
and all existing checks. Minor findings cannot be waived by an approve label.
Permissions, role deadlines and repair limits retain their existing contracts.

## Closing documentation in guarded tasks

New generated configs set `execution.documentation.enabled: true`. Configure the
optional `documenter` role explicitly through `/kh-setup-models` before starting
such a run; its starter name is not an owner choice. Existing configs that omit
the setting keep their earlier execution behavior and report `not_configured`
in the documentation receipt. Standalone `/kh-document` remains available.

An enabled planner declares `documentation` with `decision`, a concrete `reason`
and exact `paths`. Required targets must be Markdown under `app_docs/` or
`docs/agent-guide/` and fit both the original owner scope and approved plan.
Root instructions, skill files, settings and protected paths are not targets.
A necessary file outside the owner boundary remains a scope decision, not a
reason to expand permissions. Plan revisions cannot remove required targets.

When adding, changing or removing guide conditions, explicitly plan
`docs/agent-guide/conditional_docs.md` as a documentation target inside that
boundary. The documenter proposes the target with empty content. The
controller renders it from the same condition metadata before source-candidate
checks and code review. This task-scoped contract is separate from bootstrap. It never adds an
unplanned target. Unchanged conditions can retain the existing catalog on a
content-only update. Historical or superseded guides stay out of the catalog.

The configured documenter is read-only in this workflow. It proposes content
inline and writes only phase evidence. The controller checks the candidate
identity, changed-file coverage, exact approved paths, links and owner baseline.
Historical `app_docs` records use new names; they never overwrite older records.
An existing guide can be updated only when its controller-managed digest still
matches the original owner file. Owner-edited guides remain preserved verbatim.

The controller writes candidate docs before quality checks and independent code
review. The review receives the documentation decision, metadata and complete
source delta. Required docs cannot be waived by the documenter. Complete active
context must still fit the existing 128 KiB limit. Source changes, missing
coverage, malformed proposals and failed checks/review cannot authorize promotion.

After reviewed source promotion, the controller publishes only the derived
context index before recording task completion. This index projection has a
separate digest from the checked source snapshot, whose old index remains part
of its identity. Source and context publication are not one filesystem
transaction. If publication fails, the run records `publication_failed` and
reconciliation is required; it never claims completion. Inspect the retained
receipt and use reviewed context refresh as needed. A review-only continuation
uses the checked candidate and saved doc metadata without invoking a documenter.

First acceptance requires the exact reviewed document contents and live published
index. Exact replay of an already accepted completion checks the saved task/run,
plan and documentation receipts; later reviewed guide/index updates do not
invalidate that historical acceptance. A changed receipt remains rejected.
