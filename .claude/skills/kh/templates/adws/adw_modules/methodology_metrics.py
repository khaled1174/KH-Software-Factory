"""Read-only projections of controller attempts, receipts and evidence waves.

Missing historical observations stay unknown. These numbers never authorize,
schedule, retry or stop execution, and do not infer cost from blocker prose.
"""
from __future__ import annotations
import json
import math
import re


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _valid_usd(value):
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def metrics(snapshot, histories):
    tasks = snapshot.get('tasks', [])
    attempts, records = {}, {}
    incomplete_history = False
    for task in tasks:
        ident = task['id'];attempts[ident] = set()
        for event in histories.get(ident, []):
            run = event.get('run_id')
            if not isinstance(run, str) or not run:
                continue
            key = (ident, run)
            if event.get('event') == 'running':
                attempts[ident].add(run);records.setdefault(key, {})
            if event.get('event') in ('completed', 'attempt_summary'):
                records[key] = _object(event.get('detail'))
        if task.get('run_id'):
            key = (ident, task['run_id'])
            records.setdefault(key, _object(task.get('receipt')))
            if task.get('receipt') and not records[key]:
                records[key] = _object(task['receipt'])
            if task['run_id'] not in attempts[ident]:
                incomplete_history = True
    completed = [task for task in tasks if task.get('status') == 'completed']
    known_accepted = [task for task in completed if task.get('run_id') in attempts[task['id']]]
    first = sum(len(attempts[task['id']]) == 1 for task in known_accepted)
    assessed = sum(_object(_object(task.get('receipt')).get('documentation')).get('status') in ('published', 'not_needed')
                   for task in completed)
    unconfigured = sum(_object(_object(task.get('receipt')).get('documentation')).get('status') == 'not_configured'
                       for task in completed)
    costs, unknown_cost, code, evidence, unknown_repairs = [], 0, 0, 0, 0
    for record in records.values():
        cost = _object(record.get('cost'));usd = cost.get('usd')
        valid_cost = _valid_usd(usd)
        if valid_cost:
            costs.append(usd)
        if not valid_cost or cost.get('complete') is not True:
            unknown_cost += 1
        repairs = _object(record.get('execution')).get('code_repairs')
        valid_repairs = type(repairs) is int and repairs >= 0
        if valid_repairs:
            code += repairs
        handoffs = record.get('handoffs')
        if isinstance(handoffs, list):
            waves = set()
            for handoff in handoffs:
                if isinstance(handoff, dict):
                    match = re.fullmatch(r'review_(\d+)_evidence(\d+)(?:_.*)?', str(handoff.get('phase', '')))
                    if match:
                        waves.add(match.groups())
            evidence += len(waves)
        if not valid_repairs or not isinstance(handoffs, list):
            unknown_repairs += 1
    try:
        total = round(math.fsum(costs), 4)
    except OverflowError:
        total = None
    return {'schema_version': 1, 'observational_only': True,
        'attempts': None if incomplete_history else sum(len(runs) for runs in attempts.values()),
        'first_attempt_acceptance': {'accepted': first, 'accepted_with_history': len(known_accepted),
            'rate_percent': round(100 * first / len(known_accepted), 2) if known_accepted else None},
        'documentation': {'assessed_accepted_tasks': assessed, 'unconfigured_accepted_tasks': unconfigured,
            'unrecorded_accepted_tasks': len(completed) - assessed - unconfigured,
            'coverage_percent': round(100 * assessed / len(completed), 2) if completed else None},
        'repairs': {'code_observed': code, 'evidence_waves_observed': evidence, 'unknown_attempts': unknown_repairs},
        'cost': {'recorded_usd': total, 'complete': not incomplete_history and not unknown_cost and total is not None,
                 'unknown_attempts': None if incomplete_history else unknown_cost,
                 'note': 'Recorded attempt summaries/receipts only; missing prices remain partial. Cost never stops work.'}}
