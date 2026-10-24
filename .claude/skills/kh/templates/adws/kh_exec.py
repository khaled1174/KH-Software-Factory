#!/usr/bin/env python3
"""Controller-defined execution route for commands that need strict evidence (FIX-02).

    python3 adws/kh_exec.py --class test --timeout 900 -- swift test --filter X

Runs ONE program from an argv (no shell, so no pipeline, `;` or `|| true` can
hide its status), and before anything is displayed:

- writes a start record (start.json) before launch, so an interrupted command
  leaves a durable record;
- streams stdout and stderr byte-for-byte into separate files (empty streams are
  kept, with the empty SHA-256);
- records the true outcome: exit code, terminating signal, or timeout, with
  authentic timestamps and ownership-based process cleanup details.

Only the display is shortened (--display-bytes, default 20000 per stream: head
and tail are shown, the files stay complete). The last line printed is
`KH_EXEC_RECEIPT {...}`; the controller's own command record captures it at
execution time and verifies the files against it, so a file edited afterwards
no longer matches. Records go to $KH_EVIDENCE_DIR/exec/<id>/ (set by the
controller for each phase) or --out.

The exit status of kh_exec is the program's exit status (128+N for signal N,
124 for a timeout), so `&&` chains keep their meaning.
Standard library only; it runs with any python3.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MARKER = 'KH_EXEC_RECEIPT'
PIPE_GRACE_SECONDS = 5
ROLE_TAG = "KH_ROLE_TAG"
COMMAND_TAG = "KH_COMMAND_TAG"


class _BSDInfo(ctypes.Structure):
    # Darwin's struct proc_bsdinfo (PROC_PIDTBSDINFO).
    _fields_ = [("ids", ctypes.c_uint32 * 12), ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32), ("details", ctypes.c_uint32 * 6),
                ("started_sec", ctypes.c_uint64), ("started_usec", ctypes.c_uint64)]


_darwin = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True) if sys.platform == "darwin" else None
if _darwin is not None:
    _darwin.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                   ctypes.c_void_p, ctypes.c_int]
    _darwin.proc_pidinfo.restype = ctypes.c_int
    _darwin.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    _darwin.sysctl.restype = ctypes.c_int


def _process_info(pid: int):
    """Parent and kernel birth time, read together so a reused PID is distinguishable."""
    if _darwin is not None:
        info = _BSDInfo()
        if _darwin.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) == ctypes.sizeof(info):
            return info.ids[4], (info.started_sec, info.started_usec)
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), (int(fields[19]),)
    except (OSError, ValueError, IndexError):
        return None


def process_identity(pid: int):
    info = _process_info(pid)
    return info[1] if info is not None else None


def _parent_listing() -> dict[int, int]:
    try:
        listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=1).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {int(parts[0]): int(parts[1]) for line in listing.splitlines()
            if len(parts := line.split()) == 2 and all(part.isdigit() for part in parts)}


def descendants(root_pid: int, retained: dict[int, tuple] | None = None, *, root_identity=None) -> dict[int, tuple]:
    """Retain observed birth identities and follow their still-live children with one ps listing."""
    deadline = time.monotonic() + 1
    children: dict[int, list[int]] = {}
    for pid, parent in _parent_listing().items():
        children.setdefault(parent, []).append(pid)
    found = dict(retained or {})
    stack = list(found.items())
    current = process_identity(root_pid)
    if root_identity is None or current == root_identity:
        stack.append((root_pid, current))
    seen = set()
    while stack and time.monotonic() < deadline:
        parent, identity = stack.pop()
        if (parent, identity) in seen:
            continue
        seen.add((parent, identity))
        if identity is None or process_identity(parent) != identity:
            if found.get(parent) == identity:
                found.pop(parent, None)
            continue
        for child in children.get(parent, []):
            if time.monotonic() >= deadline:
                break
            info = _process_info(child)
            if (info is not None and info[0] == parent
                    and process_identity(parent) == identity):
                previous = found.get(child)
                found[child] = info[1]
                if previous != info[1]:
                    stack.append((child, info[1]))
    return found


def _environment(pid: int) -> list[bytes]:
    if _darwin is None:
        try:
            return Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return []
    # KERN_PROCARGS2 returns argc, executable, padding, argv, then the actual
    # environment. Never search ps command text: it can contain a tag in argv.
    size = ctypes.c_size_t(min(os.sysconf("SC_ARG_MAX"), 8 * 1024 * 1024))
    buffer = ctypes.create_string_buffer(size.value)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    if _darwin.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return []
    data = buffer.raw[:size.value]
    argc = int.from_bytes(data[:4], sys.byteorder)
    try:
        position = data.index(b"\0", 4) + 1
        while position < len(data) and data[position] == 0:
            position += 1
        if argc > len(data):
            return []
        for _ in range(argc):
            position = data.index(b"\0", position) + 1
        return data[position:].split(b"\0")
    except ValueError:
        return []


def tagged(tag: str, variable: str = ROLE_TAG) -> dict[int, tuple]:
    """Readable exact inherited role tags survive reparenting on Linux and macOS."""
    needle = f"{variable}={tag}".encode()
    found = {}
    try:
        pids = [int(entry) for entry in os.listdir("/proc") if entry.isdigit()] if _darwin is None else _parent_listing()
    except OSError:
        return found
    deadline = time.monotonic() + 1
    for pid in pids:
        if time.monotonic() >= deadline:
            break
        if pid == os.getpid():
            continue
        identity = process_identity(pid)
        if identity is not None and needle in _environment(pid) and process_identity(pid) == identity:
            found[pid] = identity
    return found

def process_running(pid: int, identity: tuple) -> bool:
    """A matching kernel birth identity that has not stopped or become a zombie."""
    if identity is None:
        return False
    if _darwin is not None:
        info = _BSDInfo()
        if _darwin.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            return False
        return (info.started_sec, info.started_usec) == identity and info.ids[1] != 5
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return (int(fields[19]),) == identity and fields[0] not in ('Z', 'X')
    except (OSError, ValueError, IndexError):
        return False


class CommandProcessScope:
    """Track one invocation's observed children and exact inherited ownership tag.

    Discovery is bounded and preserves birth identities after reparenting.
    This is ownership-based best-effort cleanup, not an operating-system
    sandbox: an unobserved child that removes its tag can escape discovery.
    """
    def __init__(self, env: dict, *, tag_variable: str = COMMAND_TAG):
        if tag_variable not in (COMMAND_TAG, ROLE_TAG):
            raise ValueError('process ownership requires a command or role tag')
        self.tag = uuid.uuid4().hex
        self.tag_variable = tag_variable
        self.env = {**env, tag_variable: self.tag}
        self.process = None
        self.identity = None
        self._known = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = None
        self._cleanup = None

    def start(self, process):
        self.process = process
        self.identity = process_identity(process.pid)
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _watch(self):
        while not self._closed.is_set():
            self.observe()
            self._closed.wait(.1)

    def observe(self):
        if self.process is None:
            return
        with self._lock:
            previous = dict(self._known)
        # A reaped leader's PID can be reused while a pipe holder is alive.
        # Never adopt the new process's tree as this command's descendants.
        found = descendants(self.process.pid, previous, root_identity=self.identity) if self.identity else previous
        with self._lock:
            self._known.update(found)

    def _targets(self):
        self.observe()
        found = tagged(self.tag, self.tag_variable)
        with self._lock:
            self._known.update(found)
            return dict(self._known)

    def _signal(self, targets, signum):
        process = self.process
        if process.poll() is None:
            # The group belongs to the launched session, and its leader must
            # still have the captured identity. Popen signals its own child
            # separately when that identity cannot be read.
            try:
                if (self.identity is not None and process_identity(process.pid) == self.identity
                        and os.getpgid(process.pid) == process.pid):
                    os.killpg(process.pid, signum)
                else:
                    process.send_signal(signum)
            except (ProcessLookupError, PermissionError):
                pass
        for pid, identity in targets.items():
            if pid <= 1 or pid in (os.getpid(), os.getppid()):
                continue
            if process_running(pid, identity):
                try:
                    os.kill(pid, signum)
                except (ProcessLookupError, PermissionError):
                    pass

    def stop(self, grace_seconds=.2, reap_seconds=2):
        if self._cleanup is not None and self._cleanup['complete']:
            return dict(self._cleanup)
        if self.process is None:
            return {'complete': True, 'surviving_pids': [], 'root_reaped': True, 'stopped_descendants': 0}
        targets = self._targets()
        initial = {pid for pid, identity in targets.items() if process_running(pid, identity)}
        self._signal(targets, signal.SIGTERM)
        deadline = time.monotonic() + max(0, grace_seconds)
        while time.monotonic() < deadline:
            if self.process.poll() is not None and not any(process_running(pid, identity)
                                                          for pid, identity in targets.items()):
                break
            time.sleep(.02)
        targets.update(self._targets())
        initial.update(pid for pid, identity in targets.items() if process_running(pid, identity))
        self._signal(targets, signal.SIGKILL)
        self.close()
        # The watcher can discover a child during the last signal pass. Include
        # that final observation before checking that cleanup has completed.
        targets.update(self._targets())
        self._signal(targets, signal.SIGKILL)
        deadline = time.monotonic() + max(0, reap_seconds)
        try:
            self.process.wait(timeout=max(.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        survivors = []
        while True:
            survivors = [pid for pid, identity in targets.items() if process_running(pid, identity)]
            if not survivors or time.monotonic() >= deadline:
                break
            time.sleep(.02)
        reaped = self.process.poll() is not None
        if not reaped and self.process.pid not in survivors:
            survivors.append(self.process.pid)
        self._cleanup = {'complete': reaped and not survivors, 'surviving_pids': sorted(survivors),
                         'root_reaped': reaped,
                         'stopped_descendants': len(initial - {self.process.pid} - set(survivors))}
        return dict(self._cleanup)

    def close(self):
        self._closed.set()
        if (self._thread is not None and self._thread.ident is not None
                and self._thread is not threading.current_thread()):
            self._thread.join(timeout=2.1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value) -> None:
    temporary = path.with_name('.' + path.name + '.partial')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _pump(source, target: Path, digest, counter: list, cancel, errors: list) -> None:
    """Own and close the reader; cancellation never waits for a writer's EOF."""
    try:
        with target.open('wb') as handle:
            try:
                fd = source.fileno()
                os.set_blocking(fd, False)
                while not cancel.is_set():
                    if not select.select([fd], [], [], .05)[0]:
                        continue
                    try:
                        block = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        break
                    handle.write(block)
                    digest.update(block)
                    counter[0] += len(block)
            finally:
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, ValueError) as error:
        errors.append(type(error).__name__)
    finally:
        source.close()


def _display(path: Path, limit: int, stream) -> None:
    size = path.stat().st_size
    with path.open('rb') as handle:
        if size <= limit:
            data = handle.read()
            stream.buffer.write(data)
        else:
            half = max(1, limit // 2)
            stream.buffer.write(handle.read(half))
            stream.buffer.write(f'\n… [{size - 2 * half} bytes not displayed; complete stream: {path}] …\n'.encode())
            handle.seek(size - half)
            stream.buffer.write(handle.read())
    stream.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--class', dest='klass', default='', help='declared purpose (the controller classifies '
                        'the argv itself; this label is informational)')
    parser.add_argument('--timeout', type=float, default=0, help='seconds; 0 = no limit')
    parser.add_argument('--out', default='', help='records directory (default $KH_EVIDENCE_DIR/exec)')
    parser.add_argument('--display-bytes', type=int, default=20000)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('give the program and its arguments after --')
    if not math.isfinite(args.timeout) or args.timeout < 0:
        parser.error('--timeout must be finite and nonnegative')
    base = Path(args.out or os.path.join(os.environ.get('KH_EVIDENCE_DIR') or '.', 'exec'))
    ident = f'{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")}-{uuid.uuid4().hex[:8]}'
    directory = (base / ident).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    start = {'schema_version': 1, 'id': ident, 'argv': command, 'cwd': os.getcwd(), 'class_label': args.klass,
             'timeout_seconds': args.timeout or None, 'started_at': _now(), 'pid': None}
    _write_json(directory / 'start.json', start)
    out_digest, err_digest = hashlib.sha256(), hashlib.sha256()
    out_bytes, err_bytes = [0], [0]
    outcome = {'kind': 'unknown'}
    clock = time.monotonic()
    scope = CommandProcessScope(dict(os.environ))
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                   env=scope.env, start_new_session=True)
    except OSError as error:
        outcome = {'kind': 'launch_failed', 'error': str(error), 'exit_code': 127}
        process = None
    if process is not None:
        cancel, pump_errors = threading.Event(), []
        pumps, previous_handlers = [], {}
        try:
            scope.start(process)
            start['pid'] = process.pid
            _write_json(directory / 'start.json', start)
            pumps = [threading.Thread(target=_pump, args=(process.stdout, directory / 'stdout', out_digest, out_bytes,
                                                        cancel, pump_errors),
                                      daemon=True),
                     threading.Thread(target=_pump, args=(process.stderr, directory / 'stderr', err_digest, err_bytes,
                                                         cancel, pump_errors),
                                      daemon=True)]
            for pump in pumps:
                pump.start()
            interrupted = None

            def stop(signum, _frame):
                nonlocal interrupted
                interrupted = signum
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, stop)
            deadline = clock + args.timeout if args.timeout else None
            while process.poll() is None and not interrupted:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    process.wait(timeout=.05)
                except subprocess.TimeoutExpired:
                    pass
            if interrupted:
                outcome = {'kind': 'interrupted', 'signal': interrupted, 'exit_code': 128 + interrupted}
                outcome['cleanup'] = scope.stop()
            elif process.poll() is None:
                outcome = {'kind': 'timeout', 'exit_code': 124, 'after_seconds': args.timeout,
                           'cleanup': scope.stop()}
            elif process.returncode < 0:
                outcome = {'kind': 'signal', 'signal': -process.returncode, 'exit_code': 128 - process.returncode}
            else:
                outcome = {'kind': 'exit', 'exit_code': process.returncode}
            deadline = time.monotonic() + PIPE_GRACE_SECONDS
            for pump in pumps:
                pump.join(timeout=max(0.0, deadline - time.monotonic()))
            if any(pump.is_alive() for pump in pumps):
                # The leader can exit before an owned detached pipe holder.
                # Retry cleanup, then close our readers even if EOF never comes.
                outcome['cleanup'] = scope.stop(grace_seconds=0)
                for pump in pumps:
                    pump.join(timeout=.2)
            if any(pump.is_alive() for pump in pumps) or pump_errors:
                outcome['streams_complete'] = False
                outcome['note'] = 'output capture did not reach EOF; retained streams are partial'
            if outcome.get('cleanup', {}).get('complete') is False:
                outcome['streams_complete'] = False
                outcome['note'] = 'owned command cleanup is incomplete; see surviving_pids'
        except BaseException:
            scope.stop(grace_seconds=0)
            raise
        finally:
            cancel.set()
            for pump in pumps:
                if pump.ident is not None:
                    pump.join(timeout=1)
            scope.close()
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    else:
        for name in ('stdout', 'stderr'):
            (directory / name).write_bytes(b'')
    outcome.setdefault('streams_complete', True)

    def snapshot(name):
        path = directory / name
        if not path.exists():
            path.write_bytes(b'')
        data = path.read_bytes()   # the file as it is now; what the receipt binds
        return {'file': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    receipt = {**start, 'ended_at': _now(), 'duration_seconds': round(time.monotonic() - clock, 3),
               'outcome': outcome, 'stdout': snapshot('stdout'), 'stderr': snapshot('stderr'),
               'directory': str(directory)}
    _write_json(directory / 'receipt.json', receipt)
    _display(directory / 'stdout', args.display_bytes, sys.stdout)
    _display(directory / 'stderr', args.display_bytes, sys.stderr)
    line = MARKER + ' ' + json.dumps({k: receipt[k] for k in ('id', 'argv', 'outcome', 'stdout', 'stderr',
                                                               'started_at', 'ended_at', 'directory')},
                                      separators=(',', ':'), ensure_ascii=True)
    print(line, file=sys.stderr, flush=True)
    print(line, flush=True)
    return int(outcome.get('exit_code', 1))


if __name__ == '__main__':
    raise SystemExit(main())
