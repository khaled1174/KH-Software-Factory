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
See the [report contract and coverage limits](ARCHITECTURE.md#offline-intervention-reporting).

## Configured build instruction checks

The two frozen build-defect regressions failed on `82ff7fe`: both real
`proposal_gate` results were incorrectly `passed: true` (`FAILED (failures=2)`).
After the change, `test_documentation_commands.py` passed 25 tests, covering
both real documenter gates, architect validation, a derived catalog reference,
static Makefile targets, command boundaries, canonical shell quoting and root
normalization. Historical records and optional builds remain accepted.
The independent review's seven counterexamples failed before correction
(`FAILED (failures=2, errors=5)`) and pass afterward: quoting and argument
boundaries, unrelated unreadable Makefiles and apostrophes in the project root.

The retained deterministic benchmark remains cold 10 to 10 and warm 8 to 8,
with unchanged `by_role` counts and role order. The baseline is the merged
main workflow [40452209943](https://github.com/khaled1174/KH-Software-Factory/actions/runs/40452209943);
the PR workflow uploads fresh measurements and extracted-archive logs.

On the unchanged frozen corpus, combined detection increased from 2/7 to 4/7,
with zero false alarms on six correct guides and no provider calls. The two
new rejections name the guide and declared build command. The six integrity
controls retain their expected rejections. This is stage 2A; exception taxonomy,
parser-position claims and unsupported dependency mandates remain open.

The engine remains 17,667 physical Python lines: 25 lines added and 25 removed.
Removed code is an uncalled retired-provider `UsageBreakdown.add_turn` method,
an unused route tuple and two unused imports. Active usage fields and merging,
guards, permissions, sandbox, quality floor and attempt limits are unchanged.

## Standard JSON position claims

The frozen parser-position regression failed on `55132c4`: `proposal_gate`
incorrectly returned `passed: true` (`FAILED (failures=1)`). The new check is
shared by architect and documenter validation. It parses the cited Python file,
checks the call line and supported static import, and inspects a fixed standard
JSONDecodeError instance. It never executes or imports project source.

`test_documentation_parser_facts.py` passed 24 tests; `test_quality_blocks.py`
passed six real-command tests. The latter preserve all four block operations,
600/120-second requested deadlines, failed exit/output evidence, aggregate order,
source/workable fingerprints and excluded role output. Metadata forwarding and
common spec/snapshot preparation replace duplication without changing checks.

The evaluator seeds an already-reviewed legacy snapshot using the controller's
projection, source bindings and publication writer. New bad proposals still pass
through the unchanged gate assembly. On archived main, original and revised
seeding produced identical gate reports, source snapshots, integrity controls,
labels and metrics: 4/7 detected, zero false alarms on six correct guides.
With the new runtime check, detection rises to 5/7; corpus files and labels are
unchanged. This isolates the gate improvement from the harness adjustment.

Known custom namespaces, local JSON modules/packages, role-output exclusions,
fenced examples and record-prefix qualifications have regression coverage.
The engine stays at 17,667 lines. An uncalled internal plan-export wrapper and
unused process-module imports/constant were removed; active cleanup is unchanged.
Exception generalizations and unsupported dependency mandates remain open.
Independent review found suffix qualifiers, custom error aliases/assignments,
and recursive AST rendering. Their regressions failed before correction
(`FAILED (errors=5)`). The check now matches complete cited paragraphs, tracks
both bindings, uses structural AST checks, and declines unsupported parser depth.
CRLF paragraphs and the exact trace payload fields also have regression coverage.
Oversized line references and unterminated Markdown fences are bounded without
executing project code or changing the configured retry/deadline policy.

## Owner evidence for dependency restrictions

The frozen unsupported-restriction regression failed on `2329d71`: the real
proposal gate incorrectly passed (`FAILED (failures=1)`). A generated guide now
needs a cited, affirmative owner instruction covering the guide scope. Preserved
owner guide text remains accepted; unchanged model-managed instructions cannot
self-authorize. The change shares existing architect/documenter validation.

The 19 dependency tests and 24 parser tests pass. Two additional authority
counterexamples first failed (`FAILED (failures=2)`): a nested instruction used
through path traversal and a rule qualified to one task. Both now reject.
Independent review found that mandatory instruction files override proposed
applicability with their location. Its real-proposal AGENTS/CLAUDE regression
failed before correction (`FAILED (failures=2)`) and passes afterward; scope
containment now uses the same effective applicability as publication.
Seven new controller-state contract tests pass on main and after consolidation;
the 14 existing continuous-run tests also pass. Serialization consolidation
preserves acceptance, notifications, pause/failure/interrupt payloads and locks,
freeing space for the gate while the engine stays at 17,667 lines. Guard,
permissions, sandbox, floor and attempts are unchanged.

On the unchanged offline corpus, detection increases from 5/7 to 6/7, with zero
false alarms on six correct guides, six integrity controls and zero provider
calls. Exception generalization remains open. The independent reviewer and full
source/archive verification results are reported with the PR's workflow artifacts.
Against main workflow [40470462666](https://github.com/khaled1174/KH-Software-Factory/actions/runs/40470462666),
cold calls remain 10 to 10 and warm 8 to 8. Role counts, role/target/phase order
and receipt field structure match. Host paths, run IDs, elapsed time and their
derived hashes are not compared literally. Python outside tests adds 46 lines
and removes 46; commands remain 18.

## Not verified

No live provider experiment was run for this release. Deterministic fixtures do
not establish general model quality, real-account availability or subscription
billing. The offline documentation corpus detects six of seven defective guides,
misses one, and produces zero false alarms on six correct guides; all six
integrity controls pass. These checks are not a general factual-accuracy gate.
The Claude Code settings-isolation probe uses a local mock endpoint; CI skips
that case when the CLI is unavailable. This does not test a real provider account.
CLI compatibility and sandbox backends depend on the target host. Process
cleanup is best effort for descendants that remove ownership tags. Root README
content is not tracked as a scoped guide. Retained JSON inputs are executable
test fixtures or configuration; generated evidence is outside the source tree.
