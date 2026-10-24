# Build Task

## Variables

### prompt

{{prompt}}

### plan_envelope

{{plan_envelope}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Implement the work described in `prompt` following `plan_envelope` (the accepted
plan and its acceptance criteria; it is re-sent on every attempt). When
`previous_envelope` is present it is the failed check or review that triggered
this repair attempt: fix exactly what it names, stay within the plan and the
controller's allowed paths, then emit your `Report` JSON.

## Report

Respond with ONLY valid JSON matching `BuildOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence describing what you built>",
  "changed_files": ["src/server.ts"],
  "artifacts": [],
  "commit_message": "<imperative one-line git subject for the code you changed — this is what the commit of your work will say>",
  "notes_for_next_agent": "<how to verify this work>"
}
```
