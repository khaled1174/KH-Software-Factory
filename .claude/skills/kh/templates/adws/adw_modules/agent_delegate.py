"""Pinned Codex delegate relay transport. The controller alone accepts work.

Native sandboxing covers the isolated workspace, not individual task paths.
The caller MUST isolate writes and validate their allowlist before promotion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import shutil
import uuid
import subprocess
import tempfile
import time
from typing import Callable, Optional

from .data_types import PiResult, UsageBreakdown
from .process_io import RoleTimeout
from .utils import operator_env

UPSTREAM_COMMIT = "b781ee2e23089630e2fbee1cfd6174afe4edeb76"
CODEX_EFFORT_LEVELS = ("low", "medium", "high", "xhigh")


@dataclass
class DelegateRequest:
    prompt: str
    system_prompt: str
    model: str
    session_id: str
    session_dir: str
    raw_output_path: str
    cwd: str
    baseline: str
    allowed_paths: list[str]
    evidence_requirements: list[str]
    output_contract: dict
    task_context: str
    sandbox: str = "read-only"
    sandbox_scope: list[str] = field(default_factory=list)
    tools: Optional[list[str]] = None
    extensions: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    relay_path: Optional[str] = None
    node_path: str = "node"
    thinking: str = "medium"
    supported_thinking: tuple[str, ...] = CODEX_EFFORT_LEVELS
    dispatch_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: Optional[str] = None
    phase_id: Optional[str] = None


def _relay_path(request: DelegateRequest) -> Path:
    if request.relay_path:
        return Path(request.relay_path).resolve(strict=True)
    relative = Path("vendor/delegate-skills/skills/codex-delegate/scripts/relay.mjs")
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / relative
        if candidate.is_file():
            return candidate
    raise ValueError("Pinned Codex relay missing; install the KH Software Factory vendor files")


def compatibility(relay: Path, node_path: str = "node", timeout_seconds: float = 5) -> dict:
    """Read-only local contract check; never probes provider auth or calls a model."""
    result = {"compatible": False, "authentication": "unverified", "model_access": "unverified",
              "prerequisites": {name: shutil.which(name) for name in (node_path, "codex", "git")},
              "relay": str(relay), "outcome": "unsupported_protocol"}
    if os.name != "posix" or not relay.is_file() or not all(result["prerequisites"].values()):
        return {**result, "outcome": "missing_prerequisite"}
    try:
        probe = subprocess.run([node_path, str(relay), "--handshake"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=min(5, timeout_seconds), check=False,
                               env=operator_env())
        declaration = json.loads(probe.stdout)
        expected = {"schema": "kh.delegate.handshake.v1", "result_schema": "delegate-relay.result.v1",
                    "sandbox_modes": ["read-only", "workspace-write"],
                    "effort_levels": list(CODEX_EFFORT_LEVELS),
                    "ignore_user_config": True, "approval_policy": "never"}
        if probe.returncode == 0 and declaration == expected:
            return {**result, "compatible": True, "outcome": "compatible", "contract": declaration}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return result


def doctor() -> dict:
    relative = Path("vendor/delegate-skills/skills/codex-delegate/scripts/relay.mjs")
    relay = next((a / relative for a in Path(__file__).resolve().parents if (a / relative).is_file()), relative)
    return compatibility(relay)


def classify(receipt: dict, events: list[dict], returncode: int) -> str:
    """Conservative diagnostics; failure never authorizes retry or model substitution."""
    if receipt.get("status") == "timeout":
        return "timeout_uncertain"
    if receipt.get("status") == "codex_unavailable":
        return "missing_prerequisite"
    codes = {event.get("code") for event in events if isinstance(event.get("code"), str)}
    for event in events:
        error = event.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            codes.add(error["code"])
    if codes & {"authentication_error", "invalid_api_key", "unauthorized"}:
        return "authentication_failure"
    if codes & {"permission_denied", "sandbox_violation"}:
        return "permission_violation"
    if receipt.get("signal") or receipt.get("status") == "aborted" or returncode != 0:
        return "transport_interruption"
    return "invalid_output"


def _brief(request: DelegateRequest) -> str:
    payload = {
        "controller": "KH", "controller_session_id": request.session_id,
        "dispatch_id": request.dispatch_id, "run_id": request.run_id, "phase_id": request.phase_id,
        "task_context": request.task_context, "current_request": request.prompt,
        "instructions": request.system_prompt, "working_directory": str(Path(request.cwd).resolve()),
        "source_baseline": request.baseline, "model": request.model,
        "requested_effort": request.thinking,
        "allowed_task_paths": request.allowed_paths,
        "native_sandbox": request.sandbox, "native_sandbox_scope": request.sandbox_scope,
        "required_evidence": request.evidence_requirements,
        "output_contract": request.output_contract,
        "constraints": [
            "No inherited conversation is available; use the supplied context and local evidence.",
            "KH owns planning, retries, acceptance and lifecycle. Do not start another orchestrator.",
            "Do not commit, push, publish, install skills, or expand permissions.",
            "Task paths are narrower than workspace sandboxing; the controller checks every delta before promotion.",
            "Return only the output-contract JSON. Completion is not acceptance; controller gates still apply.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate(request: DelegateRequest) -> Path:
    if os.name != "posix":
        raise ValueError("Codex delegation currently supports POSIX hosts only; Windows pilot required")
    cwd = Path(request.cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("Delegate cwd must be an existing isolated directory")
    for name in ("prompt", "system_prompt", "model", "session_id", "baseline", "task_context"):
        if not isinstance(getattr(request, name), str) or not getattr(request, name).strip():
            raise ValueError(f"Explicit delegate {name} is required")
    # This is a caller-declared capability bound, not an authenticated catalog.
    if (not isinstance(request.supported_thinking, (list, tuple))
            or not request.supported_thinking
            or any(level not in CODEX_EFFORT_LEVELS for level in request.supported_thinking)):
        raise ValueError("supported_thinking must contain supported Codex effort levels")
    if (not isinstance(request.thinking, str)
            or request.thinking not in CODEX_EFFORT_LEVELS
            or request.thinking not in request.supported_thinking):
        raise ValueError(f"Unsupported Codex thinking setting: {request.thinking!r}")
    if request.tools is not None or request.extensions:
        raise ValueError("Codex relay cannot enforce Pi/Claude tool allowlists or extensions")
    if request.sandbox not in {"read-only", "workspace-write"}:
        raise ValueError("Only read-only and workspace-write sandbox modes are supported")
    if request.sandbox == "workspace-write" and request.sandbox_scope != ["**"]:
        raise ValueError("Codex can enforce only workspace-wide writes; isolated workspace scope ['**'] required")
    if request.sandbox == "read-only" and request.sandbox_scope:
        raise ValueError("Read-only mode cannot authorize write scope")
    if not request.evidence_requirements or not isinstance(request.output_contract, dict) or not request.output_contract:
        raise ValueError("Explicit evidence requirements and output contract are required")
    if type(request.timeout_seconds) is not int or not 1 <= request.timeout_seconds <= 86400:
        raise ValueError("timeout_seconds must be an integer from 1 to 86400")
    for pattern in request.allowed_paths:
        path = Path(pattern)
        if not pattern or path.is_absolute() or ".." in path.parts:
            raise ValueError("Task paths must be nonempty repository-relative paths/globs")
    return cwd


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    # The pinned relay handles SIGTERM by terminating its detached Codex group.
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=4)


def _failure(request: DelegateRequest, artifacts: Path, error: str, code: int = 1, outcome: str = "invalid_output") -> PiResult:
    metadata = {"source_commit": UPSTREAM_COMMIT, "transport_completed": False,
                "outcome": outcome, "dispatch_id": request.dispatch_id,
                "run_id": request.run_id, "phase_id": request.phase_id, "retry_safe": False,
                "accepted": False, "acceptance_owner": "KH controller", "cost_known": False,
                "requested_effort": request.thinking, "effective_effort": None,
                "effort_verification": "unverified",
                "artifacts": str(artifacts), "error": error}
    try:
        (artifacts / "normalized.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Failure to persist diagnostics must never convert transport failure to success.
        pass
    return PiResult(returncode=code, session_id=request.session_id, terminal_success=False,
                    reported_error=error)


def normalize_result(receipt: dict, events: list[dict], returncode: int,
                     request: DelegateRequest) -> PiResult:
    """Transport validation only; output schema and acceptance remain in KH."""
    errors = []
    if returncode != 0:
        errors.append(f"Relay process exited {returncode}")
    if receipt.get("schema") != "delegate-relay.result.v1":
        errors.append("Missing or unknown relay result schema")
    if receipt.get("status") != "completed" or type(receipt.get("exitCode")) is not int or receipt["exitCode"] != 0:
        errors.append(f"Relay did not complete: {receipt.get('status')!r}")
    if receipt.get("signal") or receipt.get("error"):
        errors.append("Relay reported an error or signal")
    if receipt.get("workdir") != str(Path(request.cwd).resolve()) or receipt.get("model") != request.model or receipt.get("sandbox") != request.sandbox:
        errors.append("Relay receipt does not match requested cwd/model/sandbox")
    if receipt.get("effort") != request.thinking:
        errors.append("Relay receipt effort does not match requested thinking setting")
    final = receipt.get("finalMessage")
    if not isinstance(final, str) or not final.strip():
        errors.append("Missing final response")
    terminals = [event for event in events if event.get("type") == "turn.completed"]
    if len(terminals) != 1 or not events or events[-1].get("type") != "turn.completed":
        errors.append("Expected exactly one final turn.completed event")
    if any(event.get("type") in {"error", "turn.failed", "thread.failed"} for event in events):
        errors.append("Provider stream reported failure")
    usage = UsageBreakdown()
    if terminals:
        raw_usage = terminals[0].get("usage") or {}
        if not isinstance(raw_usage, dict):
            errors.append("Malformed provider usage")
        else:
            counts = [raw_usage.get(key, 0) for key in ("input_tokens", "cached_input_tokens", "output_tokens")]
            if any(type(n) is not int or n < 0 for n in counts) or counts[1] > counts[0]:
                errors.append("Invalid provider token counts")
            else:
                total_input, cached, output = counts
                usage.input_tokens = total_input - cached
                usage.cache_read_tokens = cached
                usage.output_tokens = output
                usage.total_tokens = total_input + output
    return PiResult(text=final if isinstance(final, str) else "", returncode=1 if errors else 0,
                    session_id=request.session_id, tokens=usage.total_tokens, usage=usage,
                    terminal_success=not errors, reported_error="; ".join(errors) if errors else False)


def run(request: DelegateRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    deadline = time.monotonic() + request.timeout_seconds
    cwd = _validate(request)
    store = Path(request.session_dir).resolve()
    store.mkdir(parents=True, exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix="delegate-", dir=store))
    try:
        relay = _relay_path(request)
    except (OSError, ValueError) as error:
        return _failure(request, artifacts, str(error), outcome="missing_prerequisite")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _failure(request, artifacts, "Delegate deadline exceeded before dispatch", 124, "transport_interruption")
    checked = compatibility(relay, request.node_path, remaining)
    (artifacts / "compatibility.json").write_text(json.dumps(checked, indent=2) + "\n", encoding="utf-8")
    if not checked["compatible"]:
        return _failure(request, artifacts, "Delegate local compatibility check failed: " + checked["outcome"],
                        outcome=checked["outcome"])
    if time.monotonic() >= deadline:
        return _failure(request, artifacts, "Delegate deadline exceeded before dispatch", 124, "transport_interruption")
    brief = artifacts / "request.json"
    brief.write_text(_brief(request), encoding="utf-8")
    cmd = [request.node_path, str(relay), "--brief", str(brief), "--cd", str(cwd),
           "--model", request.model, "--sandbox", request.sandbox,
           "--effort", request.thinking,
           "--timeout", f"{request.timeout_seconds}s", "--out-dir", str(artifacts),
           "--ignore-user-config"]
    # No shell, bypass flags, automatic provider substitution, or resume-last.
    process = None
    try:
        with (artifacts / "relay.stdout.txt").open("w", encoding="utf-8") as stdout, \
             (artifacts / "relay.stderr.txt").open("w", encoding="utf-8") as stderr:
            # The relay starts Codex with shell access: it gets the same bounded
            # environment as every other agent, never the controller's own.
            from . import infrastructure
            process = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                                       env=infrastructure.apply_worker_env(operator_env()),
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            if on_spawn:
                on_spawn(process.pid)
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _stop(process)
                message = "Delegate supervisor timed out"
                raise RoleTimeout(message, _failure(request, artifacts, message, 124, "timeout_uncertain"))
        if time.monotonic() >= deadline:
            message = "Delegate completed after its deadline"
            raise RoleTimeout(message, _failure(request, artifacts, message, 124, "timeout_uncertain"))
        receipt = json.loads((artifacts / "result.json").read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("Relay result must be an object")
        raw = (artifacts / "events.jsonl").read_text(encoding="utf-8")
        raw_path = Path(request.raw_output_path)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(raw, encoding="utf-8")
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if any(not isinstance(event, dict) or not isinstance(event.get("type"), str) for event in events):
            raise ValueError("Malformed provider event")
        result = normalize_result(receipt, events, process.returncode, request)
        metadata = {"source_commit": UPSTREAM_COMMIT, "transport_completed": result.terminal_success,
                    "accepted": False, "acceptance_owner": "KH controller",
                    "cost_known": False, "provider_thread_id": receipt.get("threadId"),
                    "outcome": "verified_result" if result.terminal_success else classify(receipt, events, process.returncode),
                    "dispatch_id": request.dispatch_id, "run_id": request.run_id, "phase_id": request.phase_id,
                    "retry_safe": False,
                    "requested_effort": request.thinking,
                    "effective_effort": receipt.get("effort"),
                    "effort_verification": "relay_receipt",
                    "artifacts": str(artifacts), "error": result.reported_error}
        (artifacts / "normalized.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        if on_event:
            for event in events:
                on_event({"type": "delegate_event", "provider": "codex", "event": event})
            on_event({"type": "delegate_result", **metadata})
        return result
    except (OSError, ValueError) as error:
        return _failure(request, artifacts, f"Delegate transport failed: {error}",
                        outcome="invalid_output" if isinstance(error, ValueError) else "transport_interruption")
    finally:
        if process is not None:
            _stop(process)
            if on_exit:
                on_exit(process.pid)
