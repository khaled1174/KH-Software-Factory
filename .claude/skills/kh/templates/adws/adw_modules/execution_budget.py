"""Controller-owned, persisted dispatch budget shared by all workspace views.

Monotonic time enforces a live deadline. Persisted wall-clock timing is evidence,
not permission to reset a run after restart; existing stage IDs are rejected.
"""
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class ExecutionBudget:
    path: Path
    policy: object
    stage: str
    started_monotonic: float = field(default_factory=time.monotonic)
    started_at: float = field(default_factory=time.time)
    substantive_sends: int = 0
    output_corrections: int = 0
    plan_repairs: int = 0
    code_repairs: int = 0
    transient_retries: int = 0
    recoveries: int = 0
    stop_reason: str = ''

    def snapshot(self):
        return dict(schema_version=1, stage=self.stage, policy=self.policy.model_dump(),
            started_at=self.started_at, deadline_at=self.started_at+self.policy.deadline_seconds,
            elapsed_seconds=max(0, time.monotonic()-self.started_monotonic),
            substantive_sends=self.substantive_sends, output_corrections=self.output_corrections,
            model_sends=self.substantive_sends+self.output_corrections,
            plan_repairs=self.plan_repairs, code_repairs=self.code_repairs,
            transient_retries=self.transient_retries, recoveries=self.recoveries, stop_reason=self.stop_reason)

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        # Session directory is controller-owned and excluded from worker writes.
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(self.snapshot(), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)


def start_budget(run, policy, stage='task'):
    if stage not in ('task', 'bootstrap'):
        raise ValueError('Unknown execution budget stage')
    path = Path(run.session_dir).resolve() / ('execution_budget-' + stage + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    if getattr(run, 'execution_budget', None) is not None:
        run.execution_budget.save()
    try:
        with path.open('x', encoding='utf-8') as handle:
            handle.write('{}')
    except FileExistsError as error:
        raise BudgetExceeded('RUN_ALREADY_STARTED: use an explicit new run ID; budgets cannot reset') from error
    budget = ExecutionBudget(path, policy, stage)
    run.execution_budget = budget
    run.execution_policy = policy
    budget.save()
    return budget


def summary(run):
    budget = getattr(run, 'execution_budget', None)
    return budget.snapshot() if budget else None


def record_stop(run, reason):
    budget = getattr(run, 'execution_budget', None)
    if budget:
        if not budget.stop_reason:
            budget.stop_reason = str(reason)
        budget.save()


def _fail(run, reason):
    record_stop(run, reason)
    raise BudgetExceeded(reason)


def remaining_timeout(run, configured_timeout):
    budget = getattr(run, 'execution_budget', None)
    if not budget:
        return configured_timeout
    if budget.stop_reason:
        raise BudgetExceeded(budget.stop_reason)
    remaining = budget.policy.deadline_seconds - (time.monotonic() - budget.started_monotonic)
    # Integer request schemas cannot represent a sub-second model timeout.
    if remaining < 1:
        _fail(run, 'EXECUTION_DEADLINE_EXCEEDED')
    return min(configured_timeout, int(remaining))


def ensure_remaining_sends(run, count):
    budget = getattr(run, 'execution_budget', None)
    if not budget:
        return
    if type(count) is not int or count < 0:
        raise ValueError('Required sends must be a nonnegative integer')
    remaining_timeout(run, budget.policy.deadline_seconds)
    if (budget.substantive_sends + count > budget.policy.max_substantive_sends or
            budget.substantive_sends + budget.output_corrections + count > budget.policy.max_model_sends):
        _fail(run, 'MODEL_SEND_BUDGET_EXHAUSTED: insufficient sends for mandatory phases')


def reserve_model_send(run, purpose='substantive'):
    budget = getattr(run, 'execution_budget', None)
    if not budget:
        return
    if purpose not in ('substantive', 'correction'):
        raise ValueError('Unknown dispatch purpose')
    remaining_timeout(run, budget.policy.deadline_seconds)
    if budget.substantive_sends + budget.output_corrections >= budget.policy.max_model_sends:
        _fail(run, 'MODEL_SEND_BUDGET_EXHAUSTED')
    if purpose == 'correction':
        if budget.output_corrections >= budget.policy.max_output_corrections:
            _fail(run, 'OUTPUT_CORRECTION_BUDGET_EXHAUSTED')
        budget.output_corrections += 1
    else:
        if budget.substantive_sends >= budget.policy.max_substantive_sends:
            _fail(run, 'SUBSTANTIVE_SEND_BUDGET_EXHAUSTED')
        budget.substantive_sends += 1
    budget.save()  # charge before transport, including uncertain failed sends


def reserve_transient_retry(run, wait_seconds):
    """Account for one provider-outage retry; refuse a wait the deadline cannot cover.

    A retry repeats a send that the provider never finished, so it spends no
    model-send or correction budget — only time, which the deadline bounds.
    """
    budget = getattr(run, 'execution_budget', None)
    if not budget:
        return
    remaining = remaining_timeout(run, budget.policy.deadline_seconds)
    if remaining <= wait_seconds + 60:
        _fail(run, 'EXECUTION_DEADLINE_EXCEEDED: no time left to wait out the provider outage')
    budget.transient_retries += 1
    budget.save()


def record_repair(run, kind):
    budget = getattr(run, 'execution_budget', None)
    if not budget:
        return
    if kind not in ('plan', 'code'):
        raise ValueError('Unknown repair kind')
    field_name = kind + '_repairs'
    limit = budget.policy.max_plan_repairs if kind == 'plan' else budget.policy.max_repairs
    if kind == 'code':
        limit += budget.recoveries * (budget.policy.max_repairs + 1)
    else:
        limit += budget.recoveries
    if getattr(budget, field_name) >= limit:
        _fail(run, kind.upper() + '_REPAIR_BUDGET_EXHAUSTED')
    setattr(budget, field_name, getattr(budget, field_name) + 1)
    budget.save()


def record_recovery(run, reason):
    """Spend one predeclared recovery, retaining every deadline/send/repair count."""
    budget = getattr(run, 'execution_budget', None)
    if budget is None or budget.recoveries >= budget.policy.recovery_rounds:
        _fail(run, 'RECOVERY_BUDGET_EXHAUSTED: ' + reason)
    remaining_timeout(run, budget.policy.deadline_seconds)
    budget.recoveries += 1
    budget.save()
