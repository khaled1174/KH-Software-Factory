"""What a role was doing when the controller cut it off.

A role that reaches its time limit is stopped from outside, so the only record
of what it was doing is the event stream it had already produced. Before this
module the stop message was the tail of stderr, which for pi is usually an
unrelated session warning, so every timeout read as something else and the real
question — was it working, or was it stuck inside one command — needed a manual
dig through raw_output.jsonl. Across one project that question came up for
about a dozen timeouts, and the answer differed: some were real work cut short,
others were a single test command that never returned.

`Activity` folds the two tool-event shapes the adapters already stream (pi's
`tool_execution_*` events and Claude Code's `tool_use` / `tool_result` blocks)
into a few facts that can be stated without guessing:

* how many tool calls the attempt made;
* the call still running at the moment of the cut, and for how long;
* otherwise the last call that finished, and how long before the cut;
* how long before the cut a file was last written.

It reads events only; it never decides anything and never touches the process.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

# Marks a stop that the controller itself imposed because the role ran out of
# time. A timeout is never a provider outage, so `transient.classify` refuses to
# treat a message carrying this marker as one, whatever else the output holds.
TIMEOUT_MARK = "ROLE TIMEOUT"

WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit",
                         "write_file", "edit_file", "apply_patch"})
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")
LABEL_CHARS = 100


def default_label(tool: str, args: object) -> str:
    """`bash: swift test --package-path app` — the call, cut to one short line."""
    value = ""
    if isinstance(args, dict):
        value = next((args[key] for key in PRIMARY_ARGS
                      if isinstance(args.get(key), str) and args[key].strip()), "")
        if not value:
            value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    if len(value) > LABEL_CHARS:
        value = value[:LABEL_CHARS].rstrip() + "…"
    return f"{tool}: {value}" if value else str(tool or "tool")


def human(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 120 else f"{seconds / 60:.1f} min"


class Activity:
    def __init__(self, label: Callable[[str, object], str] = default_label,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._label = label
        self._clock = clock
        self.calls = 0
        self._open: dict[str, tuple[str, str, float]] = {}   # id -> (tool, label, started)
        self.last_done: Optional[tuple[str, float, bool]] = None
        self.last_write: Optional[float] = None

    # -- events ----------------------------------------------------------
    def observe(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "tool_execution_start":
            self._start(event.get("toolCallId"), event.get("toolName"), event.get("args"))
        elif kind == "tool_execution_end":
            self._end(event.get("toolCallId"), event.get("toolName"), event.get("args"),
                      not event.get("isError", False))
        elif kind in ("assistant", "user"):
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                if kind == "assistant" and block.get("type") == "tool_use":
                    self._start(block.get("id"), block.get("name"), block.get("input"))
                elif kind == "user" and block.get("type") == "tool_result":
                    self._end(block.get("tool_use_id"), None, None, not block.get("is_error", False))

    def _start(self, call_id: object, tool: object, args: object) -> None:
        key = str(call_id or "")
        if not key or key in self._open:
            return
        name = str(tool or "tool")
        self.calls += 1
        self._open[key] = (name, self._label(name, args), self._clock())

    def _end(self, call_id: object, tool: object, args: object, ok: bool) -> None:
        key = str(call_id or "")
        name, label, _ = self._open.pop(key, (str(tool or "tool"), "", 0.0))
        if not label:
            label = self._label(name, args)
        now = self._clock()
        self.last_done = (label, now, ok)
        if ok and name.lower() in WRITE_TOOLS:
            self.last_write = now

    # -- the facts -------------------------------------------------------
    def describe(self, now: Optional[float] = None) -> str:
        now = self._clock() if now is None else now
        facts = [f"{self.calls} tool call{'s' if self.calls != 1 else ''} in this attempt"]
        if self._open:
            _, label, started = min(self._open.values(), key=lambda item: item[2])
            facts.append(f"still running when cut: {label} (for {human(now - started)})")
        elif self.last_done:
            label, ended, _ = self.last_done
            facts.append(f"last finished: {label} ({human(now - ended)} before the cut)")
        elif not self.calls:
            facts.append("no tool call had started")
        if self.last_write is None:
            facts.append("no file written")
        else:
            facts.append(f"last file write {human(now - self.last_write)} before the cut")
        return "; ".join(facts)


def timeout_headline(who: str, elapsed: float, limit: float, activity: Optional[Activity]) -> str:
    """The first thing anyone reads about a timeout: that it is one, and what was happening."""
    text = f"{TIMEOUT_MARK}: {who} was cut off after {int(elapsed)}s (limit {int(limit)}s)"
    if activity is not None:
        text += f"; {activity.describe()}"
    return text
