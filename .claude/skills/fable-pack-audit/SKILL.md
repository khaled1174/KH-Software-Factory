---
name: fable-pack-audit
description: Audit CLAUDE.md files, .claude/rules, skills, subagent definitions, output styles, and claude.ai instructions for patterns written for older Claude models that degrade Claude Fable 5.1 (shouted emphasis, step-by-step scripts, anti-formatting or anti-narration rules, reasoning-reproduction requests, bloat, contradictions). Use when setting up Fable 5.1, when behavior feels over-constrained, or after instruction files grow. Proposes a diff; does not apply it unless asked.
---

# Prompt audit for Claude Fable 5.1

Instructions written for earlier Claude models often hold Fable 5.1 back. Emphasis that older models needed now causes over-triggering. Step-by-step scripts are too prescriptive and lower output quality. Rules that suppressed bullets or narration now over-correct, because Fable 5.1 already formats and narrates less. The goal of this audit is fit, not brevity: remove specific dated instructions and keep context only the author knows.

## Scope

Find every instruction surface that applies here, and list them before auditing:

Resolve the user configuration root from `CLAUDE_CONFIG_DIR` when set, otherwise `~/.claude`. Use that root for all user-level paths below. Project `.claude` paths remain relative to the project.

- `~/.claude/CLAUDE.md`, `./CLAUDE.md`, `./.claude/CLAUDE.md`, `./CLAUDE.local.md`, and CLAUDE.md files in subdirectories, plus anything they import with `@path`
- `.claude/rules/**/*.md` and `~/.claude/rules/**/*.md`
- `.claude/skills/*/SKILL.md`, `~/.claude/skills/*/SKILL.md`, and legacy `.claude/commands/*.md`
- `.claude/agents/*.md` and `~/.claude/agents/*.md`
- `.claude/output-styles/*.md` and `~/.claude/output-styles/*.md`
- Any instruction text the user pastes, such as claude.ai account or project instructions

Note each CLAUDE.md file's line count. The guideline is under 200 lines per file.

## What to flag

For each finding, give the file and line, the quoted text, the category, one sentence on why it's a problem for Fable 5.1, a confidence level (high, medium, or low), and a proposed action: remove, rewrite (with the replacement text), move (and where), or add.

1. **Shouted emphasis.** Look for `CRITICAL`, `MUST`, `NEVER`, `ALWAYS`, `IMPORTANT` in caps, repeated or without a reason next to them. Rewrite at normal volume with the reason. Keep emphasis on at most one line that is demonstrably being skipped.
2. **Procedures for judgment work.** Look for numbered step scripts for tasks where order doesn't truly matter. Rewrite as outcome, constraints, and how to check. Keep exact steps only for fragile operations such as deploys, auth flows, or compliance.
3. **Dated scaffolds.** Look for "think step by step", `<thinking>` or scratchpad instructions, and "show your reasoning" or "explain your reasoning in the response". Remove these. Thinking is always on, and asking for reasoning in the response can trigger a `reasoning_extraction` refusal.
4. **Over-correcting rules.** Look for "never use bullets, headers, or bold" and "don't narrate", "hold findings for the end", or "no interim updates". Remove them, or replace with a rule saying *when* formatting or updates are wanted.
5. **Anti-laziness boosters.** Flag generic "be thorough", "don't be lazy", "if in doubt, use [tool]", and "always double-check" wording only where it causes over-scoping or redundant work. Preserve finish-the-task and persistence instructions: Fable 5.1 guidance recommends them for early stopping on long tasks. For long unattended runs, suggest a concrete verification method in addition to completion guidance.
6. **Numeric output caps.** Preserve word, character, and bullet limits that define the requested deliverable or an external format requirement. If an arbitrary global cap demonstrably omits needed information, propose narrowing its scope or describing the audience and required content. Do not claim that an answer-length limit restricts internal reasoning.
7. **Contradictions and duplicates.** Look for rules that conflict across files or say the same thing in different words. Propose one rule in one place.
8. **Content Claude can derive.** Look for directory tours, dependency lists, standard language conventions, and "write clean code". Propose removal, keeping gotchas, reasons, and conventions that differ from defaults.
9. **Unenforced guardrails.** Look for "never edit X" or "never run Y" where nothing enforces it. Propose a permission `deny` rule or hook alongside the text.
10. **Subagent cost.** Look for agent definitions with no `model` field. They inherit the main session's model, which is Fable in a Fable session. Flag them and suggest a model suited to the agent's job.
11. **Skill portability.** For skills meant for claude.ai upload, flag any frontmatter fields other than `name`, `description`, `license`, `compatibility`, `metadata`, and `allowed-tools`.
12. **Stale facts.** Look for commands, paths, or tool names that no longer exist. Verify against the repository.

## Do not flag

- Context only the author knows: audience, product, environment facts, the quality bar, and the reasons behind constraints
- Exact scripts for fragile or destructive operations
- Prohibitions tied to a real constraint or a demonstrated failure
- Skill `description` trigger wording, which may legitimately be emphatic
- A one-line role statement
- Working redundancy that isn't causing errors

## Output

Start with a summary: the files audited, finding counts by category, and the two or three findings most likely affecting behavior. Then list the findings ordered by confidence, followed by a proposed diff with one hunk per finding. Include only high- and medium-confidence findings in the diff. If the instructions are clean, say so and propose no changes. Apply edits only if the user explicitly asks.
