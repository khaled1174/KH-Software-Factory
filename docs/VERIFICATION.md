# Verification

## Baseline

Version 1.0.0 ships 18 slash commands and 12 mapped scripts. The engine contains
17,667 physical Python lines against the owner-approved ceiling of 17,667.
The deterministic sequential workload uses 10 provider calls cold and 8 warm;
these are fixture counts, not general speed or cost claims.

## Latest release gate

The full source suite and the extracted-distribution suite each completed
1,094 cases: all passed, with zero skips and zero failures. Contract evaluations passed 21/21. The installer and its
five installed project-command smoke checks passed from the extracted archive.
The trace UI passed its build, typecheck and lint checks.

`scripts/package.py` generates MANIFEST.sha256, builds kh-software-factory.zip,
checks every member hash and reruns the full suite, evaluations and installer
inside a clean extraction. Full output is retained beside the archive.
The [verification workflow](https://github.com/khaled1174/KH-Software-Factory/actions/workflows/verify.yml)
uploads the call-count baseline and full archive-verification logs. Release
publication requires that workflow to pass on the released source commit.

## Offline intervention reporting

`python3 -B -m unittest tests.test_intervention_report` passed 19 tests, including
real guarded rejection and successful repair using the deterministic provider.
The report itself is tested under an audit hook that rejects process dispatch.
Its repeated output preserves input bytes and modification times. Before the
implementation, the two baseline acceptance tests failed on source `6dd4bc4`
because `scripts/report_interventions.py` did not exist (`FAILED (failures=2)`).
After implementation, the focused suite ended with `Ran 19 tests` and `OK`.

The distribution has no local task ledger or journals. Running the report here
returns unknown stop counts, unknown human interventions and no invented history.
The seven-cause fixture is a deterministic test, not an owner usage measurement.
See the [report contract and coverage limits](INTERVENTION_REPORT.md).

## Not verified

No live provider experiment was run for this release. Deterministic fixtures do
not establish general model quality, real-account availability or subscription
billing. The offline documentation corpus detects two of seven defective guides,
misses five, and produces zero false alarms on six correct guides; all six
integrity controls pass. These checks are not a general factual-accuracy gate.
The Claude Code settings-isolation probe uses a local mock endpoint; CI skips
that case when the CLI is unavailable. This does not test a real provider account.
CLI compatibility and sandbox backends depend on the target host. Process
cleanup is best effort for descendants that remove ownership tags. Root README
content is not tracked as a scoped guide. Retained JSON inputs are executable
test fixtures or configuration; generated evidence is outside the source tree.
