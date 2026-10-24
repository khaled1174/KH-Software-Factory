---
name: astra-pack-audit
description: Audit AGENTS.md files, skills, and custom instructions for lines that make GPT-6 Astra pause, ask for confirmation, over-test, or stall. Use when Astra stops unexpectedly, asks permission it shouldn't need, or after adding or changing instruction files. Not for reviewing application code.
---

# Instruction audit

GPT-6 Astra follows instruction files more literally than earlier models. Unclear, conflicting, or overly cautious lines in AGENTS.md or skills can make it pause and leave work unfinished. This skill finds those lines and proposes fixes. It does not apply them.

## Scope

Read every instruction source that applies to the current directory:

Resolve the Codex configuration root from `CODEX_HOME` when set, otherwise `~/.codex`. Use that root for the global instruction files and config references below. Do not assume the default path is active.

- `~/.codex/AGENTS.override.md` or `~/.codex/AGENTS.md` (global; only the first non-empty one is used)
- For each directory from the repository root down to the current directory: the first file found among `AGENTS.override.md`, `AGENTS.md`, and any `project_doc_fallback_filenames` entries in the Codex config
- `SKILL.md` files under `.agents/skills` in the current directory and its parents up to the repository root, and under `~/.agents/skills`
- Also note the combined size of the AGENTS.md chain against `project_doc_max_bytes` (32 KiB by default)

List the files you found before auditing them.

## What to flag

For each finding, give the file and line, quote the text, name the category, explain in one sentence how it affects Astra, and propose replacement text or a deletion.

1. **Approval gates.** Look for "ask/confirm/check with me before…". Each one is a real pause for Astra. Decide whether it protects something irreversible, external, costly, or policy-bound. If it doesn't, propose deleting it. If it does, propose rewording it so the work is prepared first and approval is the final step, with the reason stated.
2. **Conflicts.** Look for rules that contradict each other across the global, root, nested, and skill files, or within a single file. Quote both sides and propose one rule in one place.
3. **Vague requirements.** Look for words like "appropriate", "when needed", or "be careful with X" that don't say what to do. Propose a concrete rule, or delete it.
4. **Over-testing boilerplate.** Look for "always write thorough tests" and "test everything". Propose the calibrated version: meaningful tests sized to the change, and no tests that mirror the implementation.
5. **Duplicated Codex defaults.** Look for generic autonomy, writing-style, PR-description, or "keep working after compaction" rules. Codex's built-in Astra prompt already covers these. Propose deleting them.
6. **Ambiguous approval wording.** Flag wording that actually leaves an action's authorization unclear. A skill does not need an approval declaration merely because it exists. Preserve deliberate approval requirements; clarify what may be prepared before approval and what action must wait.
7. **Conflicting precedence.** Flag skill guidance that claims to override current user instructions. Propose correcting that conflict; do not add boilerplate to every file when the harness already establishes precedence.
8. **Stale facts.** Look for commands, paths, or tool names that no longer exist in the repo. Check them by looking at the repository.

## Do not flag

- Project facts: commands, layout, conventions, environment notes
- Boundaries that carry a reason and protect real irreversible or external actions
- Code review rules that describe behavior to flag along with the safe alternative

## Output

Start with a short summary: the number of files, the number of findings per category, and the two findings most likely to be causing pauses. Then list every finding, followed by a proposed diff with one hunk per finding. Don't edit any files unless the user explicitly asks you to apply the changes.
