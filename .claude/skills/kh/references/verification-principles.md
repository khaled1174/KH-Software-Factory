# Verification principles

Four working principles for builder and reviewer sessions, adapted as concepts
from the pstack skill set (cursor/plugins, MIT) — own wording, no copied text.
They sharpen judgement; they never relax a gate, a check or the review contract.

## Prove it works

Verify against the real artifact: run the feature, read the actual value,
inspect the controller-measured diff. A proxy — "it compiles", a self-report,
a green-looking summary — is not acceptance. Every claim cites its evidence.

## Fix root causes

Reproduce first, then ask why until the symptom has a cause in the changed
code. A guard that silences a crash without removing the cause hides the next
failure. Reviewers treat symptom-masking as a blocking finding.

## Sequence verifiable units

Break multi-step work into small units that each end in a state the configured
checks can judge, and verify each before the next. A stack of unverifiable
steps is unreviewable; a stack of verified steps proves itself in order.

## Test behavior, not implementation

Call the code the way its users do and assert the observed result against a
literal expected value. If a test would still pass when the implementation
returns nothing, the assertion is theatre — rewrite it or delete it. A check
weakened to make a run green is a defect, and changing verification files
always needs a task justification.
