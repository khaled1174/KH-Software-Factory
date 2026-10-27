"""Bounded subprocess supervision with concurrent pipe draining (POSIX)."""
from __future__ import annotations
import codecs
import io
import json
import math
import select
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable
from contextlib import contextmanager

ROLE_TAG = "KH_ROLE_TAG"


class StreamResult(tuple):
    """The existing (returncode, stderr) pair plus the supervisor's cutoff fact."""
    def __new__(cls, returncode: int, stderr: str, *, timed_out: bool = False, cleanup=None, completion=None):
        result = super().__new__(cls, (returncode, stderr))
        result.timed_out = timed_out
        result.cleanup = cleanup
        result.completion = completion or {}
        return result


class RoleTimeout(RuntimeError):
    """A supervisor-imposed role cutoff, retaining the adapter's partial result."""
    def __init__(self, message: str, partial_result):
        super().__init__(message)
        self.partial_result = partial_result
        self.result = partial_result


class RoleCleanupError(RuntimeError):
    """A failed cleanup forbids another send while the owned role may be alive."""
    def __init__(self, message: str, partial_result, cleanup):
        super().__init__(message)
        self.partial_result = self.result = partial_result
        self.kh_cleanup = cleanup
        self.timed_out = partial_result.timed_out
        self.resume_safe = False


def cleanup_incomplete(value) -> bool:
    state = getattr(value, 'kh_cleanup', None)
    if state is None:
        state = getattr(value, 'cleanup', None)
    return isinstance(state, dict) and state.get('complete') is False


def require_role_cleanup(stream, partial_result):
    """Keep native exit/usage facts, but do not recover across unconfirmed cleanup."""
    if cleanup_incomplete(stream):
        partial_result.terminal_success = False
        partial_result.cost_known = False
        raise RoleCleanupError('ROLE_CLEANUP_INCOMPLETE: ' + json.dumps(stream.cleanup, sort_keys=True),
                               partial_result, stream.cleanup)


@contextmanager
def role_output(path):
    """Close role evidence without replacing a cancellation's cleanup facts."""
    raw = path.open('a', encoding='utf-8')
    try:
        yield raw
    except BaseException as error:
        try:
            raw.close()
        except BaseException as problem:
            if hasattr(error, 'add_note'):
                error.add_note('Role evidence close failed: ' + type(problem).__name__)
        raise
    else:
        raw.close()


from kh_exec import (_BSDInfo, _darwin, _process_info, process_identity,
                     _parent_listing, descendants, _environment, tagged, CommandProcessScope)


def kill_pids(pids: dict[int, tuple]) -> int:
    """Signal only the same process whose birth identity was captured during discovery."""
    killed = 0
    for pid in sorted(pids):
        if pid in (os.getpid(), os.getppid()) or pid <= 1:
            continue
        if pids[pid] is None or process_identity(pid) != pids[pid]:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    return killed


@dataclass
class TerminalCompletion:
    """Opt-in lifecycle signal; it conveys a result, never permission to accept it."""
    grace_seconds: float = 45.0
    received_at: float | None = None

    def __post_init__(self):
        if not math.isfinite(self.grace_seconds) or self.grace_seconds <= 0:
            raise ValueError('Terminal grace must be finite and positive')

    def mark(self):
        if self.received_at is None:
            self.received_at = time.monotonic()


@dataclass
class StreamRequest:
    argv: list[str]
    cwd: str
    env: dict[str, str]
    timeout: float
    # Text written to the child's stdin, then closed. None keeps stdin at
    # DEVNULL so a CLI that would wait for input sees end-of-file at once.
    input_text: str | None = None
    terminal_completion: TerminalCompletion | None = None
    stderr_observer: Callable[[str], None] | None = None


def run_stream(request, on_line, on_spawn=None, on_exit=None):
    """Own role processes and pipes until completion or bounded cancellation.

    Readable role tags and observed birth identities cover detached children.
    Discovery remains best effort for unobserved children that remove the tag.
    Callbacks always run on the caller's thread, including failure handling.
    """
    if os.name != "posix":
        raise RuntimeError("This subprocess supervisor requires POSIX process groups")
    feed = request.input_text is not None
    payload = request.input_text.encode("utf-8", errors="replace") if feed else None
    env = dict(os.environ if request.env is None else request.env)
    scope = CommandProcessScope(env, tag_variable=ROLE_TAG)
    process = None
    events, cancel = queue.Queue(), threading.Event()
    threads, worker_streams, worker_errors, cleanup_errors = [], {}, [], []
    stderr, done, timed_out = "", set(), False
    error, cleanup = None, None
    streams = []
    terminal = request.terminal_completion
    stdin_closed = threading.Event()
    if not feed:
        stdin_closed.set()
    termination = 'exited'
    drain_deadline = None
    stream_drained = False
    grace_started_at = grace_ended_at = None

    def remember_error(problem, phase):
        nonlocal error
        if error is None:
            error = problem
        elif error is not problem and hasattr(error, 'add_note'):
            error.add_note(phase + ': ' + type(problem).__name__)

    def report_worker_error(problem):
        worker_errors.append(problem)
        events.put(("failure", problem))

    def close_stream(stream):
        try:
            stream.close()
        except BaseException as problem:
            report_worker_error(problem)

    def write_input():
        # A nonblocking writer cannot retain TextIOWrapper's write lock while
        # a detached process keeps stdin open without consuming the prompt.
        try:
            fd = process.stdin.fileno()
            os.set_blocking(fd, False)
            remaining = memoryview(payload)
            while remaining and not cancel.is_set():
                if not select.select([], [fd], [], .05)[1]:
                    continue
                if cancel.is_set():
                    break
                try:
                    written = os.write(fd, remaining[:65536])
                except BlockingIOError:
                    continue
                remaining = remaining[written:]
        except BrokenPipeError:
            pass
        except BaseException as problem:
            if not cancel.is_set():
                report_worker_error(problem)
        finally:
            close_stream(process.stdin)
            if process.stdin.closed:
                stdin_closed.set()

    def drain(stream, channel):
        # Match TextIOWrapper's UTF-8 replacement and universal newlines while
        # allowing cancellation independently of a foreign writer's EOF.
        decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True)
        pending = ""
        try:
            fd = stream.fileno()
            os.set_blocking(fd, False)
            while not cancel.is_set():
                if not select.select([fd], [], [], .05)[0]:
                    continue
                if cancel.is_set():
                    break
                try:
                    block = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                pending += decoder.decode(block, final=not block)
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    events.put((channel, line + "\n"))
                if not block:
                    if pending:
                        events.put((channel, pending))
                    break
        except BaseException as problem:
            if not cancel.is_set():
                report_worker_error(problem)
        finally:
            close_stream(stream)
            events.put((channel, None))

    def stop_owned():
        nonlocal cleanup
        state = None
        for grace in (.2, 0):
            try:
                state = scope.stop(grace_seconds=grace, reap_seconds=1)
                if state['complete']:
                    break
            except BaseException as problem:
                cleanup_errors.append(type(problem).__name__)
                # A cleanup retry must not replace a real exit or supervisor
                # cutoff. Owner interruption still propagates after cleanup.
                if error is not None or not isinstance(problem, Exception):
                    remember_error(problem, 'role process cleanup')
        root_reaped = process.poll() is not None
        cleanup = state or {'complete': False, 'surviving_pids': [] if root_reaped else [process.pid],
                            'root_reaped': root_reaped, 'stopped_descendants': 0}
        if cleanup_errors:
            cleanup['errors'] = list(cleanup_errors)
        return cleanup

    def close_io():
        cancel.set()
        deadline = time.monotonic() + 1
        for thread in threads:
            if thread.ident is not None:
                try:
                    thread.join(timeout=max(0, deadline - time.monotonic()))
                except BaseException as problem:
                    remember_error(problem, 'role worker join')
        busy = {worker_streams[thread] for thread in threads if thread.is_alive()}
        for name, stream in streams:
            # A delayed worker retains its own descriptor until it observes
            # cancellation. Closing it underneath os.read/os.write could let
            # a reused descriptor refer to another role's pipe.
            if not stream.closed and stream not in busy:
                try:
                    stream.close()
                except BaseException as problem:
                    remember_error(problem, 'role ' + name + ' close')
        return {'workers_stopped': not any(thread.is_alive() for thread in threads),
                'open_streams': [name for name, stream in streams if not stream.closed]}

    try:
        process = subprocess.Popen(request.argv, cwd=request.cwd, env=scope.env,
            stdin=subprocess.PIPE if feed else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True)
        scope.process = process
        streams = [("stdout", process.stdout), ("stderr", process.stderr)]
        if feed:
            streams.append(("stdin", process.stdin))
        scope.start(process)
        threads = [threading.Thread(target=drain, args=(process.stdout, "out"), daemon=True,
                                    name=f"kh-role-{process.pid}-stdout"),
                   threading.Thread(target=drain, args=(process.stderr, "err"), daemon=True,
                                    name=f"kh-role-{process.pid}-stderr")]
        worker_streams = dict(zip(threads, (process.stdout, process.stderr)))
        if feed:
            threads.append(threading.Thread(target=write_input, daemon=True,
                                            name=f"kh-role-{process.pid}-stdin"))
            worker_streams[threads[-1]] = process.stdin
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + request.timeout
        first_output = True
        if on_spawn:
            on_spawn(process.pid)
        while len(done) < 2 or process.poll() is None:
            now = time.monotonic()
            if (grace_started_at is None and terminal is not None
                    and terminal.received_at is not None and stdin_closed.is_set()):
                grace_started_at = now
            grace_deadline = (grace_started_at + terminal.grace_seconds
                              if grace_started_at is not None else None)
            # Killing the owner is not EOF. Keep readers draining after the
            # supervised stop so queued denials and late protocol events count.
            if drain_deadline is not None:
                if now >= drain_deadline:
                    break
                remaining = drain_deadline - now
            elif now >= deadline:
                timed_out = True
                termination = 'timeout'
                grace_ended_at = now
                stop_owned()
                break
            elif grace_deadline is not None and now >= grace_deadline:
                alive = process.poll() is None
                grace_ended_at = now
                stop_owned()
                termination = 'controller_stopped_after_result' if alive else 'exited'
                drain_deadline = time.monotonic() + 2
                continue
            else:
                remaining = min(deadline, grace_deadline or deadline) - now
            try:
                channel, line = events.get(timeout=min(remaining, .1))
            except queue.Empty:
                continue
            if channel == "failure":
                raise line
            if line is None:
                done.add(channel)
            elif channel == "out":
                if first_output:
                    # Capture children before a callback can release a parent.
                    scope.observe()
                    first_output = False
                on_line(line)
            else:
                stderr = (stderr + line)[-8000:]
                if request.stderr_observer:
                    request.stderr_observer(line)
        stream_drained = len(done) == 2
        if grace_ended_at is None:
            grace_ended_at = time.monotonic()
        if not timed_out:
            # An exited root can leave detached children with closed pipes.
            # Terminal-aware callers retain ownership until these stop too.
            if terminal is not None or process.returncode:
                stop_owned()
    except BaseException as problem:
        if process is None:
            raise
        remember_error(problem, 'role execution')
        stop_owned()
    finally:
        if process is not None:
            io_state = close_io()
            for problem in worker_errors:
                remember_error(problem, 'role pipe worker')
            if (not io_state['workers_stopped'] or io_state['open_streams']) and error is None:
                remember_error(RuntimeError('ROLE_IO_CLEANUP_INCOMPLETE'), 'role pipe cleanup')
            if error is not None and (cleanup is None or not cleanup['complete']):
                stop_owned()
            # The controller's exit callback closes its live-process record.
            # An unconfirmed stop must leave a still-running leader recorded.
            if on_exit and process.poll() is not None:
                try:
                    on_exit(process.pid)
                except BaseException as problem:
                    remember_error(problem, 'role exit callback')
                    stop_owned()
            try:
                scope.close()
            except BaseException as problem:
                remember_error(problem, 'role tracking close')
                stop_owned()
            io_state = close_io()
    if terminal is not None and cleanup is not None and not timed_out and not stream_drained:
        cleanup = {**cleanup, 'complete': False, 'output_drained': False}
    if cleanup is not None:
        cleanup = {**cleanup, **io_state,
                   'complete': cleanup['complete'] and io_state['workers_stopped'] and not io_state['open_streams']}
    if error is not None:
        try:
            error.kh_cleanup = cleanup
        except Exception:
            pass
        if cleanup is not None and not cleanup['complete'] and hasattr(error, 'add_note'):
            error.add_note('ROLE_CLEANUP_INCOMPLETE: ' + json.dumps(cleanup, sort_keys=True))
        raise error
    if timed_out:
        stderr += f"\nTimed out after {request.timeout}s"
        if cleanup and cleanup['stopped_descendants']:
            stderr += f"\nStopped {cleanup['stopped_descendants']} process(es) the role left running after the timeout"
    if cleanup is not None and not cleanup['complete']:
        stderr += '\nROLE_CLEANUP_INCOMPLETE: ' + json.dumps(cleanup, sort_keys=True)
    completion = {} if terminal is None else {
        'termination': termination, 'native_returncode': process.returncode,
        'stdin_closed': stdin_closed.is_set(), 'stream_drained': stream_drained,
        'grace_seconds': terminal.grace_seconds,
        'grace_elapsed_seconds': (max(0, grace_ended_at - grace_started_at)
                                  if grace_started_at is not None and grace_ended_at is not None else None),
        'terminal_received': terminal.received_at is not None,
        'cleanup': cleanup,
    }
    return StreamResult(124 if timed_out else process.returncode, stderr,
                        timed_out=timed_out, cleanup=cleanup, completion=completion)
