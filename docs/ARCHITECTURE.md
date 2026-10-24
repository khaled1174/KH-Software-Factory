# KH architecture

The controller owns task state, retries, ordering, scope, checks and acceptance.
Role models propose typed results in separate phase contexts. Transports carry
those requests; supporting skills cannot approve work or become a second
scheduler. Owner intent, native permissions and the quality floor remain binding.

## Task lifecycle

| Stage | Result and boundary |
| --- | --- |
| Intent | Owner accepts the exact request and constraints; text identity is checked. |
| Understanding | Read-only architect proposes current guides; separate review approves them. |
| Planning | Scope, ordered steps, check obligations and documentation targets receive exact-version review. |
| Build | The builder writes an isolated candidate within the existing role boundary. |
| Documentation | A read-only documenter proposes changes to exact approved targets. |
| Verification | Actual configured commands run on the complete staged candidate. |
| Review | An independent read-only reviewer assesses actual changes, requirements and evidence. |
| Repair | Existing bounded corrections recheck changed source and carry earlier findings forward. |
| Promotion | The unchanged checked/reviewed candidate imports against the original owner baseline. |
| Completion | Derived context publishes, then the saved receipt completes the ledger task. |

An agent's SUCCESS, a check's zero exit or an approved plan alone is insufficient.
Source promotion and context publication are separate filesystem steps; a
publication failure is recorded for reconciliation rather than called complete.
See the installed [planning](../.claude/skills/kh/references/reviewed-planning.md),
[intent](../.claude/skills/kh/references/intent.md) and
[project ledger](../.claude/skills/kh/references/project-tracking.md) contracts.

## Current context and closing documentation

Current project guides live in docs/agent-guide/ and historical change records
in app_docs/. Root and nested instructions retain mandatory applicability.
The controller selects active guides; reviewed read_when conditions require
reasoned read/skip decisions for every conditional guide. Selected bytes bind
the exact plan and handoff. History does not become active context merely by
existing. Root human CHANGELOG is excluded from observed agent inputs.

The controller derives configured command facts and the conditional catalog;
the architect proposes guides rather than those derived files. Deterministic
derivation does not prove that a command ran. Active scoped guides bind observed
source content through source_digests and source_proofs. General unscoped or
universal navigation keeps existing structure-based freshness. Document bytes,
manifests, structure and owner edits retain their separate checks.

Affected scoped guides return to the existing documenter. Reviewed updates or
digest-bound confirmed_documents can establish a new source binding; a
confirmation cannot change text or metadata. Disabled or not-needed documentation
does not silently refresh a stale binding. Warm unchanged context makes no
architect call. The renewal prompt excludes the root README, so old prose there
cannot be copied merely as a renewal input. This does not exclude README from
every other role or make it a content-tracked scoped guide.

The plan's documentation.paths is the exact authorized target set.
documents[].path must match it exactly. The controller supplies documented_paths
from measured changed paths; the model does not invent the changed-file set.
Candidate identity, source proofs, target paths, lifecycle and owner baseline
are validated before staging and again at projection/promotion boundaries.
These checks establish delivery/freshness contracts, not general semantic truth.
The fixed offline corpus still misses five semantic defects; see
[verification](VERIFICATION.md#not-verified).
The full installed [context reference](../.claude/skills/kh/references/project-context.md)
defines fields, source limits, preservation, review continuity and recovery.

## Sequential execution

run-continuous executes one staged owner cycle per ready ticket, under one
repository lease. Each ticket retains reviewed planning, bounded implementation,
configured checks, documentation and an independent review before promotion.
Dependencies unlock only after acceptance in the authoritative SQLite ledger.
There are no parallel ticket workspaces, candidate imports or integration review reuse.
Ordinary task and role staging remains isolated.

Stale navigation is refreshed and independently reviewed before planning.
Existing saved-work review and bounded builder continuation retain their gates.
Required native test commands remain builder capture obligations, checked for
compatibility at plan validation. The exact owner-granted command grammar and
process-local DB overrides are defined in the installed
[test-command permissions](../.claude/skills/kh/references/test-command-permissions.md).
No container or database-server configuration change is authorized.

Historical parallel directories, execution decisions and metadata are refused
before dispatch with LEGACY_PARALLEL_STATE. Their contents are left unchanged.
Reconcile incompatible saved state explicitly; it cannot silently become
sequential authority. Read-only ledger inspection remains available.

## Transports, resources and evidence

The [delegation reference](../.claude/skills/kh/references/delegation.md) specifies
Codex relay validation, usage accounting and
provider limitations. A transport result is distinct from source acceptance.
Private subprocess ownership tracks descendants and readers; normal completion,
timeout and cancellation must settle their owned resources. Unobserved processes
that remove ownership tags retain the documented best-effort limitation.
Working-copy scope enforcement is not an operating-system/network sandbox.

Phase evidence and changed-file claims are checked on final cleaned bytes.
Native terminal completion and evidence sealing do not grant task acceptance.
Failed attempts keep their outcome and partial usage. Claude cumulative-session
usage is differenced rather than added repeatedly; subscription billing is not
inferred from estimated query prices. Failed attempts retain their trace and
partial usage. Cost is information, never a stopping threshold.

The local trace UI reads existing events and project reports. It can archive a
session's review flag but cannot dispatch or approve. Missing history remains
unknown; a fetched snapshot is not proof that an agent is alive.
See [observability](../.claude/skills/kh/references/observability.md).

## Reproduce the measured scope

```bash
python3 -B tests/test_next_call_counts.py --benchmark-contexts /tmp/kh-bench.json
```

KH_NEXT_BENCHMARK_ROOT selects an extracted comparison source. The same fixture
uses real Python/Git/SQLite/process boundaries with a deterministic provider.
The retained legacy workload dispatches 10 calls cold and 8 warm. Warm
preparation is measured separately; these counts are neither universal budgets
nor prices. Unit regressions still cover sequential documentation freshness.

Current CI artifacts bind measurements to runtime and fixture hashes. Offline
cleanup, resume and acceptance tests do not establish general model quality,
subscription compatibility or universal speed gains.
