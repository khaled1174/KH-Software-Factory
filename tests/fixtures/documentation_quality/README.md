# Offline documentation quality corpus

Seven defective guides and six correct guides cover feature state, parser
positions, build absence/omission, unsupported restrictions and exception
classification. Six integrity controls exercise actual rejection gates.

`corpus.json` binds source snapshots and complete guide text to SHA-256 hashes.
`base/` contains implementations, tests and explicit fixture requirements.
`before/` contains report stubs. Feature-state fixtures publish a reviewed guide
against those stubs, then change source while preserving guide bytes. A second
case forks A/B copies, applies A's status-count change, then checks B's retained
guide before modeled acceptance. This measures stale detection, not concurrent
production scheduling or the candidate-import protocol.

Gold labels are scoring data only. Gates receive guide text, source snapshots,
configuration and a structured proposal, without expected results. Fixed review
approvals seed a reviewed state; they do not assess model semantics.

```sh
python3 -B scripts/evaluate_documentation.py --output /tmp/kh-doc-baseline.json
python3 -B -m unittest discover -s tests -p test_documentation_evaluation.py -v
```

The output path must be new. Hash drift or failed integrity controls fail setup;
they are not counted as detections. No model, subscription or network is used.
The current mandatory handoff gates are invoked directly; this is not a full
role-acceptance test. Correct guides have unchanged source and do not measure
accurate retained prose after source drift. Snippets establish fixture ground
truth rather than adding an acceptance gate. Schema 2 carries source selection
and A/B fields; schema-1 inputs with those fields fail closed.
