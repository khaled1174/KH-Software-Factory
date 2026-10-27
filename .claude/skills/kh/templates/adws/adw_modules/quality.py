"""Controller-owned deterministic checks. Missing configuration never passes.
Configure defaults.quality_checks with argv lists; checks do not invoke a shell.
"""

from __future__ import annotations

import shlex
import subprocess
import time
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from .data_types import (EventRecord, QualityCheckResult, QualityCheckSpec, QualityResult,
                         VerifyOutput)
from .utils import now_iso, operator_env

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000
POST_STOP_DRAIN_SECONDS = 0.2


def source_fingerprint(run):
    """Identity of the source/control bytes, excluding disposable output/evidence."""
    from . import permissions, workspace
    excluded = workspace.exclusions(run)
    own = getattr(run, 'agent_output_relative', None)
    if own:
        excluded.append(own)
    captured = permissions.snapshot(SimpleNamespace(repo_root=run.repo_root, cfg=run.cfg,
                                                    snapshot_excludes=excluded))
    source = {p: digest for p, digest in captured.items() if not p.startswith('@git/')}
    return hashlib.sha256(json.dumps({'source': source, 'dependencies': permissions.dependency_fingerprint(run)},
                                     sort_keys=True).encode()).hexdigest()


def workable_fingerprint(run):
    """Identity of the part of the tree the builder may change.

    Control and configuration files (the KH install, its config, intents,
    .claude/, .kh/) and Git metadata are left out: changing a role timeout
    or a check command between runs does not make an approved plan or kept
    work stale, while any change to the code the task works on does.
    """
    from . import agents, permissions, workspace
    try:
        builder = agents.resolve(run.cfg, 'builder')
    except Exception:
        return source_fingerprint(run)
    excluded = workspace.exclusions(run)
    own = getattr(run, 'agent_output_relative', None)
    if own:
        excluded.append(own)
    captured = permissions.snapshot(SimpleNamespace(repo_root=run.repo_root, cfg=run.cfg,
                                                    snapshot_excludes=excluded))
    source = {p: digest for p, digest in captured.items()
              if not p.startswith('@git/') and permissions.permitted(p, builder, run.cfg)}
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def _configured(run, name):
    return list(run.cfg.defaults.quality_checks.get(name, [])) if hasattr(run, "cfg") else []


def require_configured(cfg, names):
    missing = [n for n in names if not cfg.defaults.quality_checks.get(n)]
    if missing:
        raise RuntimeError("NOT_CONFIGURED: configure defaults.quality_checks for " + ", ".join(missing))


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _partial_text(value):
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''


def _stop_and_capture(process, scope, partial=None, *, raw_capture=None):
    """Stop the owned command tree, then drain without waiting for detached EOF."""
    interrupted = None
    try:
        cleanup = scope.stop()
    except BaseException as error:
        if not isinstance(error, Exception):
            interrupted = error
            # Finish cleanup after an owner interrupt during the TERM grace,
            # then propagate the interrupt once its command log is retained.
            try:
                cleanup = scope.stop(grace_seconds=0)
            except BaseException as stopping_error:
                cleanup = {'complete': False, 'error': type(stopping_error).__name__}
        else:
            cleanup = {'complete': False, 'error': type(error).__name__}
        cleanup.setdefault('surviving_pids', [process.pid] if process.poll() is None else [])
        cleanup.setdefault('root_reaped', process.poll() is not None)
        cleanup.setdefault('stopped_descendants', 0)
    stdout = getattr(partial, 'output', None) or b''
    stderr = getattr(partial, 'stderr', None) or b''
    complete = False
    diagnostic = ''
    try:
        stdout, stderr = process.communicate(timeout=POST_STOP_DRAIN_SECONDS)
        complete = True
    except subprocess.TimeoutExpired as error:
        # communicate preserves its accumulated output. A still-open pipe must
        # not erase that evidence or trigger another unbounded wait.
        if error.output is not None:
            stdout = error.output
        if error.stderr is not None:
            stderr = error.stderr
    except (OSError, ValueError) as error:
        diagnostic += '\nPost-stop capture failed: ' + type(error).__name__
    except BaseException as error:
        interrupted = error
        diagnostic += '\nPost-stop capture interrupted: ' + type(error).__name__
    if raw_capture is not None:
        raw_capture.update(stdout=stdout if isinstance(stdout, bytes) else stdout.encode('utf-8'),
                           stderr=stderr if isinstance(stderr, bytes) else stderr.encode('utf-8'))
    stdout, stderr = _partial_text(stdout), _partial_text(stderr) + diagnostic
    if not cleanup.get('complete'):
        stderr += '\nCOMMAND_CLEANUP_INCOMPLETE: ' + json.dumps(cleanup, sort_keys=True)
    if not complete:
        stderr += '\nCOMMAND_STREAMS_INCOMPLETE: retained partial output; pipe draining reached its limit.'
    return stdout, stderr, cleanup, complete, interrupted


def _run(spec: QualityCheckSpec, run, *, environment=None, capture=None,
         require_cleanup=False) -> QualityCheckResult:
    phase = run.phases[-1]
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    command = shlex.join(spec.argv)
    env = operator_env() if environment is None else dict(environment)

    run.console.note(f"quality {spec.name}: {command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    cleanup = None
    streams_complete = True
    interrupted = None
    timeout = spec.timeout_seconds
    readers_closed = True
    raw_capture = {} if capture is not None else None
    configured = bool(spec.argv) and not any('PLACEHOLDER' in a for a in spec.argv)
    before = source_fingerprint(run) if configured else 'not-executed'
    if not spec.argv or any("PLACEHOLDER" in a for a in spec.argv):
        returncode = 126
        stderr = "NOT_CONFIGURED: supply a real argv command in defaults.quality_checks"
    else:
        process, scope = None, None
        try:
            from .execution_budget import remaining_timeout
            from kh_exec import CommandProcessScope
            timeout = remaining_timeout(run, spec.timeout_seconds)
            scope = CommandProcessScope(env)
            process = subprocess.Popen(spec.argv, cwd=run.repo_root, env=scope.env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=capture is None,
                start_new_session=True)
            scope.start(process)
            stdout, stderr = process.communicate(timeout=timeout)
            if raw_capture is not None:
                raw_capture.update(stdout=stdout, stderr=stderr)
                stdout, stderr = _partial_text(stdout), _partial_text(stderr)
            returncode = process.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, cleanup, streams_complete, stopping_interrupt = _stop_and_capture(
                process, scope, error, raw_capture=raw_capture)
            interrupted = stopping_interrupt
            returncode = 124
            stderr += f"\nTimed out after {timeout}s."
        except OSError as error:
            returncode = 127
            stderr = str(error)
            if process is not None and scope is not None:
                stdout, captured_stderr, cleanup, streams_complete, stopping_interrupt = _stop_and_capture(
                    process, scope, raw_capture=raw_capture)
                interrupted = stopping_interrupt
                stderr += '\n' + captured_stderr
        except BaseException as error:
            interrupted = error
            returncode = 130 if isinstance(error, KeyboardInterrupt) else 1
            if process is not None and scope is not None:
                stdout, stderr, cleanup, streams_complete, _ = _stop_and_capture(process, scope, raw_capture=raw_capture)
            stderr += '\nCommand capture interrupted: ' + type(error).__name__
        finally:
            if scope is not None:
                if require_cleanup and process is not None:
                    # Replay cannot accept while owned background work survives,
                    # even when the command leader returned successfully.
                    try:
                        cleanup = scope.stop()
                    except BaseException as error:
                        interrupted = interrupted or error
                        cleanup = {'complete': False, 'error': type(error).__name__}
                try:
                    scope.close()
                except BaseException as error:
                    interrupted = interrupted or error
                    stderr += '\nCommand tracking close interrupted: ' + type(error).__name__
                    if returncode == 0:
                        returncode = 1
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except BaseException as error:
                            interrupted = interrupted or error
                readers_closed = all(stream is None or stream.closed for stream in
                                     (process.stdin, process.stdout, process.stderr))

    if require_cleanup and (not streams_complete or not readers_closed
                            or cleanup is not None and not cleanup.get('complete')):
        returncode = returncode or 125
        stderr += '\nCOMMAND_CLEANUP_INCOMPLETE: replay cannot accept incomplete cleanup or output.'
    if require_cleanup and (not readers_closed or cleanup is not None and not cleanup.get('complete')):
        # Preserve the existing workspace-retention barrier BEFORE evidence I/O;
        # even an interrupt or failed receipt export cannot discard a live CWD.
        from . import workspace
        interrupted = interrupted or RuntimeError('NATIVE_REPLAY_CLEANUP_INCOMPLETE: retained work requires inspection')
        interrupted.kh_cleanup = {**(cleanup or {}), 'complete': False,
                                  'readers_closed': readers_closed}
        workspace.remember_role_cleanup(run, interrupted, phase_id=phase.phase_id,
                                        agent='controller_native_replay')

    after = source_fingerprint(run) if configured else before
    if before != after:
        returncode = 125
        stderr += ('\nVERIFICATION_SOURCE_CHANGED: this check modified source/control files or dependency inputs. '
                   'Run formatters or code generation in the implementation step, then verify unchanged source.')
    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"source_before: {before}\nsource_after: {after}\n"
        f"streams_complete: {str(streams_complete).lower()}\n"
        + (f"cleanup: {json.dumps(cleanup, sort_keys=True)}\n" if cleanup is not None else '') +
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    )
    if capture is not None:
        # Only the executing controller constructs this record. It is never
        # reconstructed from a worker's assertions or a pre-existing receipt.
        stdout_path, stderr_path = output_dir / 'stdout', output_dir / 'stderr'
        stdout_path.write_bytes(raw_capture.get('stdout', b''))
        stderr_path.write_bytes(raw_capture.get('stderr', b''))
        capture.update({'argv': list(spec.argv), 'cwd': str(run.repo_root),
            'timeout_seconds': timeout, 'started_at': started_at, 'ended_at': now_iso(),
            'candidate_sha256': before, 'source_after': after, 'returncode': returncode,
            'duration_seconds': duration, 'streams_complete': streams_complete,
            'readers_closed': readers_closed, 'cleanup': cleanup,
            'stdout': {'path': str(stdout_path), 'sha256': hashlib.sha256(stdout_path.read_bytes()).hexdigest()},
            'stderr': {'path': str(stderr_path), 'sha256': hashlib.sha256(stderr_path.read_bytes()).hexdigest()},
            'command_log': {'path': str(output_artifact),
                            'sha256': hashlib.sha256(output_artifact.read_bytes()).hexdigest()}})
    passed = returncode == 0
    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="tool_call",
        name=f"quality:{spec.name}",
        payload={
            "area": spec.area,
            "operation": spec.operation,
            "command": command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
            "streams_complete": streams_complete,
            "cleanup": cleanup,
        },
        started_at=started_at,
        ended_at=now_iso(),
    ))
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    if interrupted is not None:
        raise interrupted
    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


# ── Blocks ────────────────────────────────────────────────────────────────────
# Replace every argv below. See the banner at the top of this file.

def test(run) -> QualityCheckResult:
    """Run the project's test suite. The highest-value block to wire up first."""
    return _run(QualityCheckSpec(
        name="test",
        area="backend",
        operation="build",
        argv=_configured(run, "test"),        # e.g. ["bun", "test"] or ["uv", "run", "pytest", "-q"]
        timeout_seconds=600,
    ), run)


def lint(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="lint",
        area="backend",
        operation="lint",
        argv=_configured(run, "lint"),        # e.g. ["bun", "x", "oxlint@1.36.0", "src"]
    ), run)


def typecheck(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="typecheck",
        area="backend",
        operation="typecheck",
        argv=_configured(run, "typecheck"),   # e.g. ["bun", "x", "tsc", "--noEmit"]
    ), run)


def build(run) -> QualityCheckResult:
    output_dir = _check_dir(run, "build") / "bundle"
    return _run(QualityCheckSpec(
        name="build",
        area="backend",
        operation="build",
        argv=_configured(run, "build"),       # e.g. ["bun", "build", "src/index.ts", "--outdir", str(output_dir)]
    ), run)


def as_envelope(result: QualityResult, what: str) -> VerifyOutput:
    """Wrap a deterministic result so an agent can be handed it directly.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door an agent's report would — the ADW
    script is the only thing that knows the difference.
    """
    return VerifyOutput(
        status="success" if result.passed else "fail",
        summary=(f"{what}: all {len(result.checks)} check(s) passed" if result.passed
                 else f"{what}: {len(result.failures)} of {len(result.checks)} check(s) failed"),
        artifacts=result.artifacts,
        notes_for_next_agent=("" if result.passed else
                              "Repair failures attributable to this task within its scope; report unrelated failures. The output is verbatim from the "
                              "command — trust it over any summary."),
        passed=result.passed,
        failures=result.failures,
    )


def run_quality(run) -> QualityResult:
    """Run every block and collect ALL failures — one pass tells you everything.

    Ordering contract for the caller: a failing block does NOT fail the phase.
    The runner did its job; the CODE is what failed. Hand this result to the
    builder and let the bounded repair loop decide the run's fate.
    """
    available = {"test": test, "lint": lint, "typecheck": typecheck, "build": build}
    names = getattr(getattr(getattr(run, "cfg", None), "defaults", None),
                    "required_quality_checks", ["test", "lint", "typecheck", "build"])
    if not names or any(n not in available for n in names):
        raise RuntimeError("required_quality_checks must name at least one supported check")
    blocks = [available[n] for n in names]
    checks = [block(run) for block in blocks]
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )
