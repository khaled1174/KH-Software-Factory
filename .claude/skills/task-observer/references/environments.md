# Environment and authorization

Python 3.10+ with standard-library SQLite suffices for the local observer helper.
The primary KH controller calls it after task output is assessed. It is not installed
into shell startup, first-tool hooks, individual workers, or a scheduler automatically.

Use explicit project roots and quote CLI arguments. Python handles spaces and Unicode
without shell filename splitting. Symlink storage paths and evidence outside the project
are rejected. SQLite read/corruption/access errors surface as failures; callers report an
advisory storage warning while preserving the original task verdict. The helper cannot
act as an OS sandbox against a process already permitted to modify the project.

An authorization denial must not be retried through another interface. The observer
never changes permission settings. Retry only transient technical errors within the
same authorized scope and controller retry budget; otherwise preserve the error.

Optional session reminder (explicit invocation, not an installed hook):

```sh
python3 .claude/skills/task-observer/scripts/observer_store.py --project "$PWD" review-status
```

This emits valid JSON with open_count, last_review, offer_review, reason and invalid_date.
A missing date or at least seven elapsed days triggers an offer if open observations
exist. Invalid/future dates trigger a corrective offer with invalid_date=true. An unreadable
store exits nonzero instead of reporting zero observations. Existing user scheduling and
approval preferences remain authoritative; no additional approval is invented here.
