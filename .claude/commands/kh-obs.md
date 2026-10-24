---
description: "Start the existing KH trace visualization interface."
argument-hint: ""
disable-model-invocation: true
---

Resolve the target project from the current session: use its Git root or the
closest unambiguous ancestor containing `adws/`. From that project root, read
`.claude/skills/kh/references/execution-contract.md` and follow its
[standard execution contract](../skills/kh/references/execution-contract.md#standard-commands) before the action below.
This project-root rule also applies to commands installed in the user scope.

## Action

Verify that the project's justfile actually defines obs, that just and bun exist,
and that .claude/skills/kh/apps/visualizer exists in this project.
If the recipe is absent, report the missing recipe instead of overwriting the
justfile. A user-scope KH installation alone does not supply that project-local
visualizer path. Core installation intentionally excludes the optional UI.
If its package.json is absent, stop before running Bun. Report this exact
instruction, replacing PROJECT_ROOT with the resolved project path:
`python3 install.py --project "PROJECT_ROOT" --with-visualizer`
Run that installer from the complete unpacked KH distribution. It copies
source only; it does not install dependencies. Existing core conflicts require
the ordinary dry-run/upgrade review, never a silent --upgrade. Text traces
remain available through /kh-sessions and /kh-phases.

Run ["just", "obs"] from the project root using a supported background session.
This recipe installs dependencies with the committed frozen Bun lock, builds
the UI, then executes one foreground Bun server. UI and API requests share its
127.0.0.1 listener and existing Host checks. Verify actual startup output and
capture the process handle so the foreground session can be stopped cleanly.
Capture process output; report actual startup success, ports, and tool process
handles. Do not publish, open a tunnel, or change bind addresses.
The usual interface and API URL is http://localhost:4600.
No arguments are supported.

## User arguments

$ARGUMENTS

