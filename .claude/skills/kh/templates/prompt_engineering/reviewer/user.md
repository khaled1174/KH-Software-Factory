# Review Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### controller_diff

{{controller_diff}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Confirm that the work reported in `previous_envelope` is what was asked for.
Every review session must cover requirements, the complete task scope, material
correctness/security and evidence.

1. Establish the spec from the controller acceptance requirements and the prior envelope/artifacts. Your own output directory is new; do not assume the plan is inside it.
2. Read the controller-measured diff at `controller_diff` first — it is the
   authoritative task delta. `previous_envelope.changed_files` is the builder's
   claim; treat any difference between the two as a finding. Then read the
   actual source.
3. Rule on every controller requirement — copy each requirement exactly into a `findings` entry with concrete evidence.
4. Write the review to `review.md` directly inside your evidence directory, the `context_handoff_dir`
   value above and no other directory (not its parent, not a sibling phase directory). List that exact
   full path, once, in `artifacts`. Then emit your `Report` JSON.

## Report

Respond with ONLY valid JSON matching `ReviewOutput` — no prose before or after:

```json
{
  "status": "success",
  "approved": false,
  "summary": "<one sentence: N of M requirements met>",
  "findings": [
    { "requirement": "<the ask, in the requester's words>", "met": true, "evidence": "src/server.ts:42 — handler registered" }
  ],
  "blocking": ["<what must change before this can be approved>"],
  "artifacts": ["<the context_handoff_dir value above>/review.md"],
  "notes_for_next_agent": "<what the builder must fix, or how to verify if approved>"
}
```

`status` is `success` when the review itself completed — it is not the verdict. The verdict is `approved`, and it is true only when `findings` has no unmet entry and `blocking` is empty.

For a scoped task, also return `scope_findings` for every controller-supplied
changed source path: `{"path":"src/example.py","within_scope":true,"reason":"Concrete relation to the requested behavior"}`.
Use the complete controller list rather than only the last repair's file list.
If any change violates the non-goals, return approved=false and explain the
required correction. Necessary supporting work within the task is not drift.
When the controller lists changed verification files (tests, check configs),
the matching acceptance requirement needs evidence that they were not weakened,
skipped or deleted to make checks pass.
