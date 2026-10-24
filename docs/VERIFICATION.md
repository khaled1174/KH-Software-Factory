# Verification

## Baseline

Version 1.0.0 ships 18 slash commands and 12 mapped scripts. The engine contains
17,658 physical Python lines against the owner-approved ceiling of 17,667.
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
