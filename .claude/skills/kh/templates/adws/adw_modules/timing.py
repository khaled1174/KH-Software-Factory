"""Check the deadline against the work the policy permits (SYS-04).

A run was stopped in its fourth review: three full cycles had used most of the
10,800 s deadline, and a review that measured 1,300-1,530 s could not fit. The
deadline was a constant; nothing compared it with the work the repair rounds
permit, and nothing refused to start a review that could not finish.

This module keeps the observed duration of each kind of phase
(.kh/timing-history.json, controller-owned; no worker workspace contains it):

- before a run, `forecast` compares the deadline with planning + every
  permitted build/check/review cycle at observed durations and reports whether
  the limit supports the workflow (`execution.timing_preflight`: warn, refuse
  or off). The owner's limit is never raised or reset automatically;
- during a run, `require` refuses to begin a build cycle or a review that the
  remaining time cannot finish, judged only from this run's own observed
  durations (no stop before the run has measured that kind of phase). The candidate is kept, with its last real
  verdict and any completed checks, in an explicit unreviewed state
  (DEADLINE_INSUFFICIENT / CANDIDATE_CHECKED_UNREVIEWED) instead of being cut
  off mid-review. A cut-off review never becomes a verdict.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

HISTORY = Path('.kh') / 'timing-history.json'
KEEP = 20
KINDS = ('planning', 'build', 'checks', 'review')


class InsufficientTime(RuntimeError):
    pass


def _all(root) -> dict:
    try:
        data = json.loads((Path(root) / HISTORY).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(root, mode: str = 'full') -> dict:
    """Observed durations of one workflow mode (light and full runs differ in size)."""
    try:
        data = _all(root).get(mode) or {}
        return {k: [float(x) for x in data.get(k, [])][-KEEP:] for k in KINDS}
    except (TypeError, AttributeError, ValueError):
        return {k: [] for k in KINDS}


def record(root, kind: str, seconds: float, mode: str = 'full') -> None:
    if kind not in KINDS or seconds <= 0:
        return
    path = Path(root) / HISTORY
    everything = _all(root)
    data = load(root, mode)
    data[kind] = [*data[kind], round(float(seconds), 1)][-KEEP:]
    everything[mode] = data
    data = everything
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        pass                      # timing evidence is advisory; never fail a task over it


def estimate(history: dict, kind: str, observed=()) -> float | None:
    """This run's longest observation, else the historical median, else unknown."""
    if observed:
        return max(observed)
    values = history.get(kind) or []
    return statistics.median(values) if values else None


def forecast(root, policy, lenses: int, planning_needed: bool = True) -> dict:
    history = load(root, getattr(policy, 'mode', 'full'))
    cycle_parts = {k: estimate(history, k) for k in ('build', 'checks', 'review')}
    planning = estimate(history, 'planning') if planning_needed else 0.0
    cycles = policy.max_repairs + 1
    known = all(v is not None for v in cycle_parts.values()) and planning is not None
    result = {'deadline_seconds': policy.deadline_seconds, 'permitted_cycles': cycles, 'review_sessions_per_cycle': lenses,
              'basis': 'median of observed runs in .kh/timing-history.json' if known else
              'no complete history yet: the forecast becomes available after the first runs record durations',
              'observations': {k: len(history.get(k, [])) for k in KINDS}}
    if not known:
        result.update(forecast_seconds=None, supported=None)
        return result
    per_cycle = cycle_parts['build'] + cycle_parts['checks'] + cycle_parts['review'] * lenses
    total = planning + per_cycle * cycles
    result.update(per_cycle_seconds=round(per_cycle), planning_seconds=round(planning),
                  forecast_seconds=round(total), supported=total <= policy.deadline_seconds,
                  cycles_that_fit=max(0, int((policy.deadline_seconds - planning) // per_cycle)) if per_cycle else cycles)
    return result


def preflight(root, cfg, policy, lenses: int, planning_needed: bool = True) -> dict:
    result = forecast(root, policy, lenses, planning_needed)
    mode = cfg.execution.timing_preflight
    result['mode'] = mode
    if mode == 'refuse' and result.get('supported') is False:
        raise ValueError(f"TIMING_PREFLIGHT: the deadline of {policy.deadline_seconds}s supports "
                         f"{result['cycles_that_fit']} of {result['permitted_cycles']} permitted build/check/review "
                         f"cycles at observed durations ({result['forecast_seconds']}s needed). Raise "
                         "execution.deadline_seconds or lower max_repairs explicitly; KH never changes either.")
    return result


class Clock:
    """Per-run observations, used to refuse work the remaining time cannot finish."""

    def __init__(self, root, mode: str = 'full', record: bool = True):
        self.root, self.mode, self.persist = root, mode, record
        self.observed = {k: [] for k in KINDS}

    def done(self, kind: str, seconds: float) -> None:
        self.observed[kind].append(seconds)
        if self.persist:
            record(self.root, kind, seconds, self.mode)

    def need(self, kinds) -> float | None:
        """Only this run's own observations: other tasks' durations are no basis to stop this one."""
        total = 0.0
        for kind, count in kinds:
            value = max(self.observed[kind]) if self.observed[kind] else None
            if value is None:
                return None
            total += value * count
        return total


def remaining(run) -> float | None:
    budget = getattr(run, 'execution_budget', None)
    if budget is None:
        return None
    return budget.policy.deadline_seconds - (time.monotonic() - budget.started_monotonic)


def require(run, clock: Clock, kinds, state: str, what: str) -> None:
    """Refuse to begin work the remaining time cannot finish."""
    if clock is None or getattr(getattr(run, 'cfg', None), 'execution', None) is None:
        return
    if run.cfg.execution.timing_preflight == 'off':
        return
    left = remaining(run)
    needed = clock.need(kinds)
    if left is None or needed is None or left >= needed:
        return
    raise InsufficientTime(f'{state}: {int(left)}s remain, {int(needed)}s are needed to {what} at observed '
                           'durations; the candidate is kept unreviewed for the next run instead of being '
                           'cut off mid-phase')
