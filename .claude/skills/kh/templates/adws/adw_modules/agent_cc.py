"""Claude Code coding agent interface.

Runs `claude -p --output-format stream-json --verbose` and tails its NDJSON
stdout line by line, forwarding each event to a callback WHILE the agent works
— the shared streaming result contract over Claude Code's wire format.

Sessions: `--session-id <uuid>` creates, `--resume <uuid>` continues. A marker
file under the agent's session dir records which state this adapter is in, so
JSON retries and gate corrections re-enter the SAME context window — exactly
create-or-continue session behavior, implemented with two flags
and a file.

Translation table (KH vocabulary -> claude CLI):

  tools     read/bash/edit/write/grep/find/ls -> Read/Bash/Edit/Write/Grep/Glob/LS,
            passed as --tools. None (unset) means all tools, as before.
  thinking  off|minimal|low|medium|high|xhigh|max -> CLAUDE_CODE_EFFORT_LEVEL,
            set per subprocess (env beats session setting, so each agent keeps
            its own level in one shared install). `off` additionally exports
            MAX_THINKING_TOKENS=0. The env var, not the newer --effort flag:
            an unknown env var is ignored, an unknown flag kills the run.
  system prompt  --append-system-prompt, not --system-prompt. Claude Code's own
            base prompt carries its tool-use conventions; the agent's identity
            is appended to it rather than replacing it.
  permissions  --permission-mode acceptEdits plus --settings. Every role runs
            in a fresh isolated copy that Claude Code has never trusted, so in
            -p mode it ignores the project's .claude/settings*.json and, under
            `default`, refuses even the evidence file each role must write.
            acceptEdits accepts edits inside the working directory (the copy)
            only. Edits anywhere else still need a permission no one can give,
            and the controller's scope check decides what reaches the owner's
            tree. The owner's own allow/deny/ask rules are carried in a
            generated settings file: edit grants are dropped (inside the copy
            they are redundant, outside it they would bypass isolation), and
            settings-relative `/path` rules are anchored to the copy. The file
            lives outside the copy, beyond the agent's edit reach.
  extensions  legacy harness_engineering TS files are not loadable here. MCP
            servers and hooks live in Claude Code's own settings; a non-empty
            list is reported as a trace event, not silently dropped.
  isolation  a role is a fresh, scoped session, not the owner's Claude Code.
            Without flags it would inherit everything installed for the
            owner's account: user MCP servers, claude.ai connectors, plugins,
            skills and slash commands, user agents, the user CLAUDE.md and
            hooks. On a busy machine that is hundreds of tool schemas sent
            with every request (one reviewer spent 318K tokens before reading
            a file) and tools no role was given (mail, databases, docs).
            ISOLATION_FLAGS load project and local settings only, no MCP
            server, no skill or slash command, no Chrome integration; the
            generated settings file also turns claude.ai connectors off.
            Sign-in is unaffected (OAuth and keychain are not settings). An
            owner whose sign-in or model setup lives in ~/.claude/settings.json
            (apiKeyHelper, env) sets KH_CC_INHERIT_USER_SETUP=1 in .env to
            go back to inheriting it all.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional
from pydantic import Field

from .activity import Activity, timeout_headline
from .data_types import CCRequest, PiResult, UsageBreakdown, _valid_cost
from .utils import now_iso, operator_env

CC_PATH = os.environ.get("CLAUDE_CODE_PATH", "claude")

RESULT_SNIPPET_CHARS = 20_000   # tool output rides along whole; clip only guards pathological cases
ARG_VALUE_CHARS = 20_000        # args too — the UI scrolls, it must not be handed cut-off data
LABEL_CHARS = 80                # "bash: <command>" shown as the event name

# The arg that identifies a call at a glance, in the order tools tend to use.
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")

# KH roster names -> Claude Code built-in tool names. `find` maps to Glob:
# The roster's find is a glob matcher; Claude Code calls it Glob.
TOOL_MAP = {
    "read": "Read",
    "bash": "Bash",
    "edit": "Edit",
    "write": "Write",
    "grep": "Grep",
    "find": "Glob",
    "ls": "LS",
}

# The roster's reasoning ladder onto Claude Code's effort levels. `off` has no effort
# equivalent — it is handled with MAX_THINKING_TOKENS=0 and maps here only so
# the env still carries a sane floor.
EFFORT_MAP = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# Measured with Claude Code 2.1.296 against a recording API endpoint: with
# these flags a user MCP server, user skill, user agent and user CLAUDE.md no
# longer reach the request; without them all four do.
ISOLATION_FLAGS = [
    "--strict-mcp-config",
    "--setting-sources", "project,local",
    "--disable-slash-commands",
    "--no-chrome",
]
ISOLATION_SETTINGS = {"disableClaudeAiConnectors": True}
SECRET_PATHS = ("~/.ssh", "~/.aws", "~/.config/gh")
SANDBOX_SETTINGS = {
    "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
    "autoAllowBashIfSandboxed": False,  # Keep the owner's existing command approvals.
    "network": {"allowedDomains": [], "deniedDomains": ["*"], "strictAllowlist": True},
    "filesystem": {"denyRead": list(SECRET_PATHS)},
}
INHERIT_ENV = "KH_CC_INHERIT_USER_SETUP"


def isolated() -> bool:
    return os.environ.get(INHERIT_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


DEFAULT_CONTEXT_WINDOW = 200_000   # Claude's standard ceiling; [1m] variants differ


def preflight() -> list[str]:
    """What validate() asks: is the binary there, before anything spawns."""
    if shutil.which(CC_PATH):
        return []
    return [f"claude CLI not found (looked for {CC_PATH!r} on PATH — "
            "install Claude Code or set CLAUDE_CODE_PATH in .env)"]


def resolve_model(pattern: str) -> str:
    """Resolve a config model to the id/alias claude --model takes.

    Claude Code has no catalog to resolve against: it accepts aliases
    (`opus`, `sonnet`, `haiku`) and full ids verbatim. A `provider/` prefix is
    stripped (`anthropic/claude-opus-5` -> `claude-opus-5`) so rosters written
    in provider/id form keep working. Whatever is left is passed through
    unchanged — a typo fails at spawn, in the trace, not in a guess here.
    """
    model = pattern.strip()
    if not model:
        raise ValueError("empty model id")
    if "/" in model:
        model = model.split("/", 1)[1]
    return model


def window_override() -> int:
    """The window the operator pinned with KH_CC_CONTEXT_WINDOW, else 0."""
    override = os.environ.get("KH_CC_CONTEXT_WINDOW", "").strip()
    return int(override) if override.isdigit() and int(override) > 0 else 0


def context_window(model_id: str) -> int:
    """The window assumed BEFORE a send has reported its own.

    Claude Code does not announce a ceiling up front, so until a send finishes
    this is the operator's pin if there is one, else the standard 200K. 0 would
    mean 'unknown' to the UI's context bar; a sane default serves better. The
    finished send replaces it with what the CLI reports (`reported_window`),
    because the assumption was wrong in practice: the models in use report
    1,000,000, and a reviewer measured against 200K read as 327-360% full while
    actually at 65-72%, which ended its task with SCOPE_TOO_LARGE_FOR_REVIEW.
    """
    return window_override() or DEFAULT_CONTEXT_WINDOW


def reported_window(event: object, model_id: str) -> int:
    """The context window the CLI itself reported for this send, else 0.

    The `result` event carries `modelUsage` keyed by model id, each entry with a
    `contextWindow`. A send can touch more than one model (a helper model may
    appear next to the requested one), so the entry is chosen by name; with
    several entries and no name match nothing is guessed.
    """
    usage = event.get("modelUsage") if isinstance(event, dict) else None
    if not isinstance(usage, dict):
        return 0
    windows = {}
    for name, entry in usage.items():
        value = entry.get("contextWindow") if isinstance(entry, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            windows[str(name)] = value
    if not windows:
        return 0
    wanted = model_id.split("[", 1)[0].strip().lower()
    for name, value in windows.items():
        base = name.split("[", 1)[0].strip().lower()
        # Equal, a dated variant of the same id, or an alias (`sonnet`) inside the id.
        if wanted and base and (wanted in base or base in wanted):
            return value
    return next(iter(windows.values())) if len(windows) == 1 else 0


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _label(tool: str, args: dict) -> str:
    """One-line human name for a tool call: `bash: ls -la src`."""
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    return f"{tool.lower()}: {_clip(value, LABEL_CHARS)}" if value else tool.lower()


def _blocks(event: dict) -> list:
    """The content blocks of an assistant/user stream event, always a list."""
    content = (event.get("message") or {}).get("content", [])
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _result_text(block: dict) -> str:
    """A tool_result's content arrives as a string or as content blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


class ToolCallTracker:
    """Folds Claude Code's stream into ONE normalized record per completed call.

    Claude Code announces a call as a `tool_use` block inside an `assistant`
    event, and reports it as a `tool_result` block inside the following `user`
    event. Only the result closes the call, so that is where a record is
    emitted — one trace event per real tool call, the moment it returns.

    A shared record shape: the tracer and the UI never learn which
    coding agent produced them.
    """

    def __init__(self, on_open=None, on_close=None) -> None:
        self._open: dict[str, dict] = {}
        # Unclipped call data for the controller's command ledger (SYS-01).
        self._on_open, self._on_close = on_open, on_close

    def observe(self, event: dict) -> list[dict]:
        """Returns the record for a finished tool call, else None."""
        etype = event.get("type", "")
        if etype == "assistant":
            for block in _blocks(event):
                if block.get("type") == "tool_use":
                    self._announce(block.get("id"), block.get("name"),
                                   block.get("input"))
            return []
        if etype != "user":
            return []

        records = []
        for block in _blocks(event):
            if block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            opened = self._open.pop(call_id, {})
            tool = str(opened.get("tool") or "tool")
            args = opened.get("args") or {}
            record = {
                "tool": tool,
                "tool_call_id": call_id,
                "args": {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                         for key, value in args.items()},
                "ok": not block.get("is_error", False),
                "label": _label(tool, args),
            }
            result_text = _result_text(block)
            if self._on_close:
                self._on_close(call_id, tool, args, result_text, not block.get("is_error", False))
            if result_text:
                record["result_snippet"] = _clip(result_text, RESULT_SNIPPET_CHARS)
            record["ended_at"] = now_iso()
            if opened.get("clock"):
                record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
            if opened.get("started_at"):
                record["started_at"] = opened["started_at"]
            records.append(record)
        return records

    def _announce(self, call_id, tool, args) -> None:
        """First sighting starts the clock; a later sighting only fills gaps."""
        if not call_id:
            return
        known = self._open.get(str(call_id), {})
        if not known and self._on_open:
            self._on_open(str(call_id), tool, args or {})
        self._open[str(call_id)] = {
            "tool": tool or known.get("tool", ""),
            "args": args or known.get("args", {}),
            "started_at": known.get("started_at") or now_iso(),   # wall clock, for the row
            "clock": known.get("clock") or time.monotonic(),      # monotonic, for duration
        }


def _usage_parts(usage: dict) -> dict | None:
    """Claude Code's usage keys onto the shared UsageBreakdown vocabulary."""
    if not isinstance(usage, dict):
        return None
    parts = {
        "input": usage.get("input_tokens") or 0,
        "output": usage.get("output_tokens") or 0,
        "cacheRead": usage.get("cache_read_input_tokens") or 0,
        "cacheWrite": usage.get("cache_creation_input_tokens") or 0,
        "cost": {},
    }
    if any(type(parts[k]) is not int or parts[k] < 0 for k in ('input', 'output', 'cacheRead', 'cacheWrite')):
        return None
    return parts


def _session_marker(request: CCRequest) -> Path:
    """One file per session id: exists == Claude Code knows this session."""
    marker_dir = Path(request.session_dir)
    marker_dir.mkdir(parents=True, exist_ok=True)
    return marker_dir / f"{request.session_id}.created"


# Rules for these tools name paths; Read rules also cover Grep and Glob, Edit
# rules every edit tool (Claude Code permission semantics).
PATH_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead", "Glob", "Grep", "LS"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _rule_tool(rule: str) -> str:
    return rule.split("(", 1)[0].strip()


def _anchor(rule: str, root: Path) -> str:
    """`Tool(/x)` is relative to the project that holds the settings file: pin it to `root`."""
    tool, sep, rest = rule.partition("(")
    if sep and tool.strip() in PATH_TOOLS and rest.startswith("/") and not rest.startswith("//"):
        return f"{tool}(//{str(root).lstrip('/')}{rest}"
    return rule


def owner_permissions(cwd) -> dict:
    """The owner's project permission rules, made safe to apply inside the isolated copy."""
    root = Path(cwd).resolve()
    merged = {"allow": [], "deny": [], "ask": []}
    for name in ("settings.json", "settings.local.json"):
        path = root / ".claude" / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            permissions = json.loads(path.read_text(encoding="utf-8")).get("permissions") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for kind in merged:
            rules = permissions.get(kind) if isinstance(permissions, dict) else None
            for rule in rules if isinstance(rules, list) else []:
                if not isinstance(rule, str) or not rule.strip():
                    continue
                if kind == "allow" and _rule_tool(rule) in EDIT_TOOLS:
                    continue
                merged[kind].append(_anchor(rule.strip(), root))
    return {kind: list(dict.fromkeys(rules)) for kind, rules in merged.items() if rules}


def _command(request: CCRequest, resume: bool, settings_path: Optional[str] = None) -> list[str]:
    cmd = [
        CC_PATH, "-p", "--output-format", "stream-json", "--verbose",
        "--model", resolve_model(request.model),
        "--permission-mode", "acceptEdits",
        "--append-system-prompt", request.system_prompt,
        "--resume" if resume else "--session-id", request.session_id,
    ]
    if isolated():
        cmd += ISOLATION_FLAGS
    if settings_path:
        cmd += ["--settings", settings_path]
    if request.tools is not None:
        cmd += ["--tools", ",".join(TOOL_MAP.get(t, t) for t in request.tools)]
    # `--tools` is variadic in the Claude Code CLI: a prompt placed after it is
    # read as another tool name. `--` ends option parsing, which also keeps a
    # prompt that starts with "-" from being read as an option. A prompt too
    # large for one argv string (Linux caps each at 128 KiB) goes via stdin.
    if not prompt_via_stdin(request):
        cmd += ["--", request.prompt]
    return cmd


# Linux MAX_ARG_STRLEN is 131072 bytes per argument; keep clear of it.
PROMPT_ARGV_LIMIT = 100_000


def prompt_via_stdin(request: CCRequest) -> bool:
    return len(request.prompt.encode("utf-8")) > PROMPT_ARGV_LIMIT


def run(request: CCRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive Claude Code turn.

    `on_spawn(pid)` and `on_exit(pid)` bracket the child process so the caller
    can record it as killable — a hung coding agent is otherwise a pid you have
    to hunt for in `ps` while the run sits there.

    A `--resume` against a session Claude Code has never seen fails with "No
    conversation found" — the marker file and Claude's own store can disagree
    after a manual cleanup. That case falls back to creating the session, once,
    because a correction loop dying on a bookkeeping mismatch is a self-inflicted
    wound.
    """
    marker = _session_marker(request)
    result = _run_once(request, resume=marker.exists(),
                       on_event=on_event, on_spawn=on_spawn, on_exit=on_exit)
    if not result.timed_out and result.returncode != 0 and "No conversation found" in result.stderr_tail:
        first = result
        marker.unlink(missing_ok=True)
        from .claude_usage import reset
        reset_saved = reset(request)
        result = _run_once(request, resume=False,
                           on_event=on_event, on_spawn=on_spawn, on_exit=on_exit)
        _combine_attempts(first, result)
        if not reset_saved:
            result.accounting.setdefault('warnings', []).append('native_counter_reset_not_persisted')
            result.accounting['complete'] = result.cost_known = False
    elif not result.timed_out and result.returncode != 0 and "already in use" in result.stderr_tail:
        # The reverse mismatch: a first send that failed after Claude Code had
        # created the session (a provider outage, say) left no marker, so the
        # repeat asked to create it again. Continue the session it already has.
        first = result
        result = _run_once(request, resume=True,
                           on_event=on_event, on_spawn=on_spawn, on_exit=on_exit)
        _combine_attempts(first, result)
    if result.returncode == 0 and result.terminal_success and not result.reported_error:
        try:
            marker.touch()
        except BaseException as error:
            result.terminal_success = result.cost_known = False
            result.accounting.update(complete=False, provider_estimated_cost_complete=False)
            result.accounting.setdefault('warnings', []).append('session_marker_not_persisted')
            from .claude_usage import write_receipt
            write_receipt(request, result)
            error.kh_claude_usage_result = result
            raise
    if result.returncode != 0 or result.reported_error or not result.terminal_success:
        if result.timed_out:
            # A role that ran out of time says so first, with what it was doing.
            from .process_io import RoleTimeout
            result.terminal_success = False
            result.cost_known = False
            raise RoleTimeout(f"claude exited {result.returncode}: {result.timeout_note}; "
                              f"{result.stderr_tail}", result)
        raise CCRunError(f"claude exited {result.returncode}: {result.stderr_tail}", result)
    return result


def _combine_attempts(first, result):
    """Session-recovery dispatches remain charged even when the last one wins."""
    result.tokens += first.tokens
    result.usage.merge(first.usage)
    combined = first.cost + result.cost
    if not _valid_cost(combined):
        combined = first.cost
        result.cost_known = False
    result.cost = result.usage.total_cost = combined
    # A rejected session-lookup with no usage events spent no measured work.
    if first.accounting:
        result.cost_known = result.cost_known and (first.cost_known or
            (first.tokens == 0 and first.cost == 0 and not first.accounting.get('observed_native_usage')))
    result.accounting['fallback_attempts'] = [first.accounting, dict(result.accounting)]
    result.accounting['provider_estimated_cost_usd'] = combined
    result.accounting['provider_estimated_cost_complete'] = result.cost_known


class CCRunError(RuntimeError):
    """Preserve already observed consumption without treating failure as success."""
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def _run_once(request: CCRequest, resume: bool,
              on_event: Optional[Callable[[dict], None]],
              on_spawn: Optional[Callable[[int], None]],
              on_exit: Optional[Callable[[int], None]]) -> "CCRunResult":
    from .process_io import cleanup_incomplete
    permissions = owner_permissions(request.cwd)
    from . import infrastructure
    deny = [*infrastructure.claude_deny_rules(),
            *(f"Read({path}{suffix})" for path in SECRET_PATHS for suffix in ("", "/**"))]
    if deny:
        permissions = {**permissions, "deny": list(dict.fromkeys([*permissions.get("deny", []), *deny]))}
    settings = dict(ISOLATION_SETTINGS) if isolated() else {}
    settings["sandbox"] = SANDBOX_SETTINGS
    if permissions:
        settings["permissions"] = permissions
    holder = tempfile.mkdtemp(prefix="kh-claude-settings-") if settings else None
    pending = False
    try:
        settings_path = None
        if holder:
            settings_path = str(Path(holder) / "settings.json")
            document = json.dumps(settings, indent=2)
            Path(settings_path).write_text(document, encoding="utf-8")
            # Evidence copy of exactly what this send was granted (not the file in use).
            evidence = Path(request.raw_output_path).with_name("claude-settings.json")
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(document, encoding="utf-8")
        return _run_command(request, _command(request, resume, settings_path), on_event, on_spawn, on_exit,
                            fresh=not resume)
    except BaseException as error:
        pending = cleanup_incomplete(error)
        if holder and pending:
            error.retained_policy_directory = holder
            error.kh_cleanup.setdefault('retained_paths', []).append(holder)
        raise
    finally:
        if holder and not pending:
            shutil.rmtree(holder, ignore_errors=True)


def _run_command(request: CCRequest, cmd: list[str],
                 on_event: Optional[Callable[[dict], None]],
                 on_spawn: Optional[Callable[[int], None]],
                 on_exit: Optional[Callable[[int], None]], *, fresh: bool = True) -> "CCRunResult":

    from . import infrastructure
    env = infrastructure.apply_worker_env(operator_env())
    env["CLAUDE_CODE_EFFORT_LEVEL"] = EFFORT_MAP.get(request.thinking, request.thinking)
    if request.thinking == "off":
        env["MAX_THINKING_TOKENS"] = "0"
    if isolated():
        env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    model_id = resolve_model(request.model)
    result = CCRunResult(session_id=request.session_id,
                         context_window=context_window(model_id))
    from .claude_usage import ClaudeUsage
    accounting = ClaudeUsage(request, fresh)
    activity = Activity()
    started = time.monotonic()
    # stdin carries the prompt only when it is too large for argv; otherwise it
    # is DEVNULL, deliberately — an inherited non-TTY stdin can park the child
    # forever waiting for input that will never arrive.
    from .process_io import StreamRequest, run_stream, require_role_cleanup, role_output, cleanup_incomplete
    try:
        with role_output(raw_path) as raw:
            def receive(line):
                raw.write(line)
                raw.flush()                      # events land on disk as they happen
                line = line.strip()
                if not line:
                    return
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    return
                activity.observe(event)
                accounting.observe(event)
                accounting.apply(result)
                etype = event.get("type")
                if etype == "assistant":
                    message = event.get("message", {})
                    text = "".join(block.get("text", "") for block in _blocks(event)
                                   if block.get("type") == "text")
                    if text:
                        result.text = text       # last assistant message wins
                    usage = message.get("usage", {}) or {}
                    if usage:
                        parts = _usage_parts(usage)
                        turn = sum(parts[k] for k in ("input", "output", "cacheRead", "cacheWrite")) if parts else 0
                        # Occupancy is read off the last assistant turn — the size
                        # of what the next turn would start from.
                        if turn:
                            result.context_tokens = turn
                elif etype == "result":
                    result.terminal_success = event.get("subtype") == "success" and not event.get("is_error", False)
                    result.session_id = event.get("session_id") or result.session_id
                    final_text = event.get("result")
                    if isinstance(final_text, str) and final_text:
                        result.text = final_text
                    if event.get("is_error") or event.get("subtype") not in (None, "success"):
                        result.reported_error = True
                    # The CLI states the window it ran with. Use it unless the
                    # operator pinned one; the guard reads this send's result next.
                    reported = reported_window(event, model_id)
                    if reported and not window_override():
                        result.context_window = reported
                if on_event:
                    on_event(event)

            stream = run_stream(
                StreamRequest(cmd, request.cwd, env, request.timeout_seconds,
                              input_text=request.prompt if prompt_via_stdin(request) else None), receive,
                on_spawn=on_spawn, on_exit=on_exit)
            result.returncode, stderr = stream
            result.timed_out = bool(getattr(stream, 'timed_out', False))
            result.stderr_tail = stderr[-800:]
            if (fresh and result.returncode != 0 and 'already in use' in result.stderr_tail
                    and not cleanup_incomplete(stream)):
                accounting.reject_creation()
            accounting.finish(result, interrupted=result.timed_out or cleanup_incomplete(stream))
            require_role_cleanup(stream, result)
            if on_event:
                on_event({'type': 'claude_usage_accounting', **result.accounting})
            if result.timed_out:
                result.timeout_note = timeout_headline("claude", time.monotonic() - started,
                                                       request.timeout_seconds, activity)
    except BaseException as error:
        result.terminal_success = False
        accounting.finish(result, interrupted=True)
        error.kh_claude_usage_result = result
        raise

    return result


class CCRunResult(PiResult):
    """PiResult plus the stderr tail, for the resume-fallback check above."""

    stderr_tail: str = ""
    reported_error: bool = False
    # Set when the controller cut the send off: what the role was doing then.
    timeout_note: str = ""
    accounting: dict = Field(default_factory=dict)
