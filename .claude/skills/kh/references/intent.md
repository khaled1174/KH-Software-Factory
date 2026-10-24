# Intent capture before planning

KH starts execution from a task. The intent artifact is the committed record
*upstream* of that task: what is wanted, why, and under which constraints, in
the originator's own terms, accepted by the owner before any plan is drafted.
It closes the gap where an excellent plan serves the wrong task.

## The chain

```
originator's words -> intent.md (draft) -> owner accepts -> /kh-plan-review
                   -> reviewed planning -> guarded build -> verified promotion
```

Each stage ends with a saved artifact and the next begins by reading it.
The intent is the first link: it joins the task's evidence chain alongside the
plan, the build delta, and the review record.

## Rules

1. Copy `.claude/skills/kh/templates/intent.md` into the project's `intents/`
   directory, one Markdown file per change. Fill `## Task request` with the
   exact task request from the plan or command, including internal whitespace.
   Exactly one intent may match a request.
2. The owner changes `Status: draft` to `Status: accepted` and fills `Accepted by`
   and `Accepted on` (YYYY-MM-DD) in `## Acceptance`, plus `Approved sensitive
   changes` (`none`, or owner-review categories this task may promote; see
   [config](config.md#owner-review-reported-after-completion)).
   That is the owner's whole step; nothing else is asked before or during the
   run. The first guarded run that uses the accepted intent records the
   SHA-256 of its text in `.kh/intent-digests.json`, and every later run
   compares against it, so any edit after acceptance — including to the
   approvals — voids the acceptance until the owner re-accepts with a new
   `Accepted on` (or `Accepted by`). An owner who wants to pin the text from
   the start may add `Accepted digest: <sha256 of the file without that line>`
   under `## Acceptance`; an explicit line takes precedence over the record,
   and a misplaced or repeated line is refused. Previews, planning and dry runs
   read the record but never write it. The receipt records the digest and how
   it was bound (`intent_binding`: `explicit`, `first-use` or `recorded`).
   This is a recorded human decision bound to its text, not a cryptographic
   attestation of who wrote it: an agent running as the owner's
   operating-system user can write the same line or record, and text edited
   between acceptance and the first run is what gets bound. It catches drift,
   not forgery.
3. The Python entry points for guarded startup and reviewed planning read and
   validate that accepted intent before any model dispatch, including direct
   CLI and continuous project execution. The plan and reviewer receive its
   content and hash as part of the frozen context.
4. If implementation reveals the intent was wrong, revise the intent and
   re-accept it before the plan changes. The digest enforces this: a revised
   intent is refused until the owner re-accepts it.
5. Scope contracts still bind what files may change; the intent binds *why*.
   Neither substitutes for the other.

The runtime refuses a missing, ambiguous, malformed, unaccepted,
request-mismatched, or edited-after-acceptance intent. It rejects symbolic links
and oversized intent files. Existing projects must add accepted intents before
starting their next task. Intents accepted before the digest existed are bound
on their next guarded run, with no extra step.

## Measuring it

- **Leading**: time from the first conversation about a change to an accepted
  intent, compared with the previous clarify-as-you-go cycle.
- **Lagging**: revisions to an intent dated after the first plan commit for
  the same change. A rising count means planning starts before intent is
  stable — treat it as a process finding, not a writing problem.
