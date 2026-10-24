#!/bin/bash
# PreToolUse hook: blocks Edit/Write/MultiEdit/NotebookEdit on paths listed in .claude/protected-paths.txt.
# One path per line, relative to the project root. Lines starting with # are ignored.
# A directory entry protects everything under it.

if ! command -v python3 >/dev/null 2>&1; then
  echo "Blocked: protected-path check requires python3." >&2
  exit 2
fi

python3 -c '
import json
import os
from pathlib import Path
import sys

def within(path, parent):
    return path == parent or parent in path.parents

try:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).resolve()
    entries = (root / ".claude/protected-paths.txt").read_text().splitlines()
    payload = json.load(sys.stdin)
    tool_input = payload["tool_input"]
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input must be an object")
    # Edit/Write/MultiEdit pass file_path; NotebookEdit passes notebook_path.
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("file_path or notebook_path must be a non-empty path")
    entries = [line.strip() for line in entries]
    entries = [line for line in entries if line and not line.startswith("#")]
    # An empty or comment-only list is a broken configuration, not an empty
    # protection set: fail closed rather than silently permit every edit.
    if not entries:
        print("Blocked: .claude/protected-paths.txt has no effective entries. Repair the hook configuration before retrying.", file=sys.stderr)
        sys.exit(2)
    path = Path(raw)
    # Relative tool paths use the invocation working directory.
    path = path if path.is_absolute() else Path.cwd() / path
    lexical = Path(os.path.abspath(path))
    resolved = path.resolve()
    # macOS filesystems compare case-insensitively; Linux stays case-sensitive
    # so a differently-cased path is not falsely blocked. KH_HOOK_PLATFORM
    # overrides the detected platform so tests can exercise this from any host.
    fold = os.environ.get("KH_HOOK_PLATFORM", sys.platform) == "darwin"
    if fold:
        lexical = Path(str(lexical).casefold())
        resolved = Path(str(resolved).casefold())
    for entry in entries:
        configured = Path(entry)
        if configured.is_absolute() or ".." in configured.parts:
            raise ValueError("protected entries must be project-relative without ..")
        protected = root / configured
        absolute = Path(os.path.abspath(protected))
        resolved_protected = protected.resolve()
        if fold:
            absolute = Path(str(absolute).casefold())
            resolved_protected = Path(str(resolved_protected).casefold())
        if within(lexical, absolute) or within(resolved, resolved_protected):
            print("Blocked: protected path. Do not work around this restriction; report the conflict and continue independent work.", file=sys.stderr)
            sys.exit(2)
except (OSError, ValueError, TypeError, KeyError, RuntimeError):
    print("Blocked: could not validate the tool path or protected-paths configuration. Repair the hook configuration before retrying.", file=sys.stderr)
    sys.exit(2)
'
status=$?
# Hook errors must block rather than silently permit an edit.
if [ "$status" -ne 0 ]; then
  exit 2
fi
