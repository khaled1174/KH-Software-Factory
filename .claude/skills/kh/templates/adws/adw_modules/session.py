"""Session lifecycle: pin-or-create an adw_id, build the Run object.

`ensure(cfg, adw_id)` joins the session if it exists or creates it under
exactly that id (pinned ids for repeatable runs); omitted, a fresh id is
minted and printed so the next ADW can pick it up.
"""

from __future__ import annotations

import os
import re
import time
import atexit
from . import git_helper
import signal
import sys
import stat
from pathlib import Path

from .data_types import KHConfig
from .runner import Run
from .tracer import Tracer
from .utils import engineer_name, new_id


def _finalize_when_killed(run: Run) -> None:
    """A killed run still closes its own trace.

    Python's default SIGTERM handling exits without unwinding, so a termination signal would leave the session reading `running` forever and
    its process rows open — the trace would claim work is in flight that is
    already dead. Turning the signal into SystemExit both finalizes here and
    lets the phase context manager record the phase as failed on the way out.
    """
    def handler(signum, _frame):
        # SystemExit still has to unwind role cleanup before agent rows can close.
        run.tracer.session_finish(run.adw_id, ok=False, preserve_agents=True)
        raise SystemExit(128 + signum)

    run._previous_signal_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def restore_signal_handlers(run):
    for sig, handler in getattr(run, '_previous_signal_handlers', {}).items():
        signal.signal(sig, handler)
    run._previous_signal_handlers = {}


class RepoLease:
    """A borrowed lease never releases the batch controller's writer lock."""
    def __init__(self, stream, root, *, owned=True):
        self.stream, self.root, self.owned = stream, Path(root).resolve(), owned
    @property
    def closed(self): return self.stream.closed
    def close(self):
        if self.owned: self.stream.close()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def borrow(self, root):
        if self.closed or self.root != Path(root).resolve():
            raise RuntimeError('Invalid or closed repository lease')
        return RepoLease(self.stream, self.root, owned=False)


def acquire_repo_lock(root: Path, *, wait_seconds=0):
    """One writer; bounded, cancellable waiting, never a background scheduler."""
    import fcntl
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
        raise ValueError('wait_seconds must be an integer in 0..60')
    root = Path(root).resolve()
    control = root / '.kh'
    control.mkdir(mode=0o700, exist_ok=True)
    parent = os.open(control, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open('controller.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise RuntimeError('Refusing non-regular repository lock')
    stream = os.fdopen(descriptor, 'a+')
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('BUSY: Another KH controller is active in this repository')
                time.sleep(min(0.1, remaining))
    except BaseException:
        stream.close()
        raise
    lease = RepoLease(stream, root)
    atexit.register(lease.close)
    return lease


def ensure(cfg: KHConfig, adw_id: str | None = None, *, repo_lock=None, fresh=False) -> Run:
    adw_id = adw_id or new_id(8)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", adw_id):
        raise ValueError("Invalid session ID: use letters, digits, underscore, hyphen")
    root = git_helper.repo_root()
    lock = repo_lock.borrow(root) if isinstance(repo_lock, RepoLease) else acquire_repo_lock(root)
    if repo_lock is not None and not isinstance(repo_lock, RepoLease):
        lock.close()
        raise ValueError('Repository lock must be a controller lease')
    directory = Path(cfg.defaults.data_dir) / 'sessions' / adw_id
    if fresh and (directory.exists() or directory.is_symlink()):
        lock.close()
        raise RuntimeError('RUN_ALREADY_STARTED: use a new run ID; budgets and approvals cannot reset')
    try:
        tracer = Tracer(cfg.observability.db,
                        f"{cfg.defaults.data_dir}/sessions/{adw_id}/events.jsonl")
        run = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer=engineer_name())
        run._lock = lock
        tracer.session_start(adw_id, run.engineer, adw_name=Path(sys.argv[0]).stem)
        # This process is the run. Record it before any phase opens, so a run that
        # hangs in its first agent call is still killable by adw_id.
        tracer.process_start(adw_id, "adw", "", os.getpid(),
                             " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]))
        _finalize_when_killed(run)
        run.console.session_started(adw_id, run.engineer)
        return run
    except BaseException:
        lock.close()
        raise
