# Offline intervention baseline and blocker report

Run this inspector from the distribution against an explicitly selected project:

```bash
python3 -B scripts/report_interventions.py --root "/path/to/project"
python3 -B scripts/report_interventions.py --root "/path/to/project" --format json
```

Output goes to stdout. No project files, ledger rows, reports or approvals are
written. The inspector imports no KH runtime and starts no subprocess or model.
It is a distribution maintenance script, not an installed slash command.

## Inputs and counting

The inspector reads `.kh/project.db` and the default
`adws/adw_data/sessions/*/events.jsonl` journals. It does not search other
projects, custom data directories, environment files or provider output streams.
Input paths and SHA-256 hashes identify the records used by the report.

Use a quiescent offline project. SQLite reads use a private temporary copy;
nonempty WAL or rollback-journal files are refused instead of silently omitting
uncheckpointed changes. Symlinked and nonregular input files are refused.
Corrupt inputs produce exit code 2, an explicit error and no partial report.

Each ledger `blocked` or `recovered` event is one recorded stop. A guarded
terminal journal error adds a stop only when its run has no ledger stop. A
missing ledger stop can be recovered observationally from that journal without
writing it back. Repaired role errors and gate failures are not terminal stops.
Repeated report generation does not add observations or restart work.

The baseline distinguishes these causes using explicit controller markers:

| Cause | Recorded signal |
| --- | --- |
| `owner_decision` | `OWNER_DECISION_REQUIRED` or `OWNER_REVIEW_REQUIRED` |
| `permissions` | A role's measured prohibited-path rejection |
| `quality` | Failed role gates, rejected saved review/checks, plan revision or changed verification source |
| `environment` | `INFRA_OR_PROVIDER_BLOCKER` or `INFRA_BOUNDARY` |
| `interruption` | A ledger `recovered` event |
| `limits` | Explicit budget, deadline, role timeout or no-progress stop |
| `unknown` | No recognized controller signal |

These counts measure recorded stops, not the number of times a human intervened.
`human_interventions` remains null because the input records do not establish it.
Free-form text mentioning HTTP errors, permissions or a possible remedy is not
enough to classify a cause. Unknown history is never reconstructed from guesses.

## Blocker delivery

Each stop retains its reason, task/run identity, recorded timestamp, evidence
location and hash. A matching journal can supply the failed phase, gate and its
violations. An unrelated or missing journal cannot supply that attribution.
Missing phase, gate or affected-file evidence stays unknown; the inspector does
not infer a file from the task's allowed paths.

`open_blockers` is derived from the current ledger status. Historical stops stay
visible after a successful retry but no longer appear as open blockers. Without
a ledger, current task status and `open_blockers` are unknown. Missing stop
history for a blocked task marks coverage as partial and retains its recorded
snapshot instead of reporting zero stops.

The required-action text points to the existing guarded correction, owner
acceptance or recovery path. It grants no permissions, resets no counters and
records no decision. Existing `report`/`hold`, notification failure and task
acceptance behavior is unchanged. Evidence links are local; generated reports
can contain private paths and recorded messages and should stay with the project.

The JSON format is deterministic for unchanged inputs. Missing history uses null
counts; an initialized unstarted project instead has zero recorded stops. Custom
data paths, pre-session failures and unrecorded manual actions remain outside
this baseline. A zero observed count is not a claim that no human work occurred.
