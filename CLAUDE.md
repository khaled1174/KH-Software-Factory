@AGENTS.md

## Working in this repo (Claude Code)

If, while working or testing, you find a pre-existing bug, a performance concern, or behavior the task doesn't mention, don't fix, optimize or extend it in this change unless the requested behavior cannot work without it; report it as a follow-up in your summary. Where the task is ambiguous, implement the reading its wording and the surrounding code most directly support, state that assumption in your summary, and don't build for the other readings as well. Verify your work however you like; scratch scripts and quick checks need not be kept. Commit tests only where the task asks for them or this repository already keeps tests for this kind of change, sized like the neighboring test files — roughly one focused test per stated behavior — and don't turn scratch checks into additional permanent test files. This is about extras only: implement every behavior the task asks for, completely.

The number of tokens used to edit files is best minimized, all else being equal. Therefore, when it will not affect the end result, try to surgically edit a file rather than rewrite the entire thing.

Before reporting progress, audit each claim against a tool result from this session. Only report work you can point to evidence for; if something is not yet verified, say so explicitly. Report outcomes faithfully: if tests fail, say so with the output; if a step was skipped, say that; when something is done and verified, state it plainly without hedging.

## Protected paths

A PreToolUse hook (`.claude/hooks/protect-paths.sh`) blocks edits to the paths
listed in `.claude/protected-paths.txt`: the manifest, the vendored relay,
and the enforcement mechanism itself (`.claude/settings.json`,
`.claude/hooks/`, `.claude/protected-paths.txt`). If a task genuinely requires
one of them, stop and ask the owner; do not work around the block. A hook
failure blocks by design.

The hook governs development of this repository only; it is not installed
into target projects, which are bounded by `defaults.protected_files` as an
acceptance check after the fact. The matcher covers Edit, Write, MultiEdit
and NotebookEdit only, so a write made through Bash reaches a protected path.
