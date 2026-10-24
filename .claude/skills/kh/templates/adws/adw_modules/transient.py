"""Tell a provider outage from a real failure, so a send can wait and go again.

A send that died because the provider was overloaded, at capacity, rate
limited for a moment, or because the connection dropped says nothing about the
work; repeating the same prompt in the same session after a pause is the
whole remedy. Everything else — a timeout the controller imposed, a refused
key, an exhausted quota or usage limit, a bad request, a gate or budget stop —
is not retried here.

Only text that reports an error is read: the transport exception, stderr, and
the events in the attempt's own output that are error events. Model prose is
never matched, so a reviewer discussing "503" in the code it reviews cannot
turn its own failure into a retry.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .activity import TIMEOUT_MARK
from .process_io import RoleTimeout, cleanup_incomplete

TRANSIENT = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\b(?:429|500|502|503|504|529)\b[^\n]{0,40}(?:too many requests|internal server error|bad gateway|"
        r"service unavailable|gateway time-?out|overloaded|unavailable)",
        r"(?:status|http|code|api error|api_error_status)[\"':= ]{0,4}(?:429|500|502|503|504|529)\b",
        r"overloaded", r"at capacity", r"service unavailable", r"temporarily unavailable",
        r"server_error", r"rate[ _-]?limit", r"too many requests", r"try again later",
        r"fetch failed", r"econnreset", r"econnrefused", r"etimedout", r"eai_again", r"socket hang up",
        r"stream disconnected", r"connection (?:reset|refused|error|closed)", r"network error",
        r"premature close", r"und_err_", r"other side closed",
        # A fetch aborted from below the agent (the request never got a reply):
        # pi reports it as stopReason "error" with this message. A run the
        # owner interrupts stops the controller itself, not a single request.
        r"operation was aborted",
    )
]
# These outlast any pause the controller would take: retrying only burns time.
PERMANENT = [
    re.compile(p, re.IGNORECASE) for p in (
        r"quota", r"usage limit", r"billing", r"credit balance", r"insufficient[_ ](?:funds|quota|credits)",
        r"invalid[_ ]api[_ ]key", r"authenticat", r"unauthori[sz]ed", r"\b401\b", r"\b403\b",
        r"permission[_ ]denied", r"prompt is too long", r"context (?:length|window)", r"model[_ ]not[_ ]found",
        r"not supported", r"invalid[_ ]request",
    )
]
OUTPUT_FILES = ("raw_output.jsonl", "result.json", "relay.stderr.txt", "normalized.json")
TAIL_BYTES = 32768


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _error_part(event) -> str:
    """The event when it reports an error; '' for model output and progress."""
    if not isinstance(event, dict):
        return ""
    kind = str(event.get("type", ""))
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if ("error" in kind or "failed" in kind or event.get("is_error") or event.get("error")
            or event.get("api_error_status") or event.get("severity") == "error"
            or message.get("stopReason") == "error" or message.get("errorMessage")):
        if message:
            # The error, not the assistant text that came with it.
            event = {k: v for k, v in event.items() if k != "message"}
            event["message"] = {k: message.get(k) for k in ("stopReason", "errorMessage") if k in message}
        return json.dumps(event, ensure_ascii=False)[:4000]
    return ""


def error_text(error: BaseException | str, attempt_dir: Path | None) -> str:
    parts = [str(error)]
    if attempt_dir is not None and Path(attempt_dir).is_dir():
        for path in sorted(Path(attempt_dir).rglob("*")):
            if path.name not in OUTPUT_FILES or not path.is_file():
                continue
            text = _tail(path)
            if path.suffix in (".jsonl", ".json"):
                lines = text.splitlines() if path.suffix == ".jsonl" else [text]
                for line in lines:
                    try:
                        parts.append(_error_part(json.loads(line)))
                    except ValueError:
                        continue
            else:
                parts.append(text)
    return "\n".join(part for part in parts if part)


def classify(error: BaseException | str, attempt_dir: Path | None = None) -> str:
    """A short reason when the failure is a provider outage worth waiting out, else ''."""
    if isinstance(error, RoleTimeout) or cleanup_incomplete(error):
        return ""
    if TIMEOUT_MARK in str(error):
        # The controller stopped the role because its time ran out. Nothing the
        # provider did explains that, and an aborted request earlier in the same
        # output (or a command whose own text says "rate limit") must not turn
        # it into an outage to wait out.
        return ""
    text = error_text(error, attempt_dir)
    if any(p.search(text) for p in PERMANENT):
        return ""
    for pattern in TRANSIENT:
        found = pattern.search(text)
        if found:
            return found.group(0)[:120]
    return ""


def delay(attempt: int, base_seconds: int) -> int:
    """Wait before retry number `attempt` (1-based): base, 2x base, 4x base, ..."""
    return int(base_seconds * (2 ** max(0, attempt - 1)))
