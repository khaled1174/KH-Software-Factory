"""Attribute native cumulative Claude usage without importing session history.

Terminal modelUsage includes helper models and supersedes deduplicated assistant
observations. Prices are provider estimates; subscription billing is unknown.
This meter has no execution, permission, acceptance or monetary-stop authority.
"""
from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path

from .data_types import UsageBreakdown, _valid_cost
from .scope import _atomic_json, _read_json

PARTS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')
FIELDS = (*PARTS, 'reasoning_tokens')
NATIVE = ('inputTokens', 'outputTokens', 'cacheReadInputTokens', 'cacheCreationInputTokens')
ASSISTANT = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


def zero():
    return {key: 0 for key in FIELDS}


def add(left, right):
    return {key: left[key] + right[key] for key in FIELDS}


def counts(value, keys, *, optional_cache=False):
    if not isinstance(value, dict):
        raise ValueError('usage is not an object')
    result = zero()
    for index, (target, source) in enumerate(zip(PARTS, keys)):
        item = value.get(source, 0 if optional_cache and index >= 2 else None)
        if type(item) is not int or item < 0:
            raise ValueError('usage count is not a nonnegative integer')
        result[target] = item
    thinking = value.get('thinkingTokens', 0) if keys == NATIVE else (
        value.get('output_tokens_details') or {}).get('thinking_tokens', 0)
    if type(thinking) is not int or not 0 <= thinking <= result['output_tokens']:
        raise ValueError('thinking is not a subset of output')
    result['reasoning_tokens'] = thinking
    return result


def state_path(request):
    return Path(request.session_dir) / (request.session_id + '.usage.json')


def empty(session_id, fresh):
    return {'schema_version': 1, 'session_id': session_id, 'known_zero_baseline': fresh, 'created': False,
        'models': None, 'settled': zero(), 'reported': zero(), 'seen': {}, 'covered': {},
        'price_baseline': 0.0 if fresh else None, 'price_attributed': 0.0,
        'price_reported': 0.0, 'warnings': []}


def reset(request):
    """A confirmed missing native conversation starts a new counter generation."""
    try:
        _atomic_json(state_path(request), empty(request.session_id, True))
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def write_receipt(request, result):
    try:
        _atomic_json(Path(request.raw_output_path).with_name('claude-usage.json'),
            {**result.accounting, 'usage': result.usage.model_dump(), 'tokens': result.tokens})
    except (OSError, ValueError, RuntimeError):
        result.accounting['warnings'] = list(dict.fromkeys([
            *result.accounting.get('warnings', []), 'usage_receipt_not_persisted']))
        result.accounting['complete'] = False


class ClaudeUsage:
    def __init__(self, request, fresh):
        self.request = request
        self.path = state_path(request)
        self.state = empty(request.session_id, fresh)
        self.warnings = []
        self.unwritable = False
        self.observed = False
        self.snapshot_valid = False
        self.price_valid = False
        self.terminals = {}
        self.loaded = False
        self.reasoning_coverage = False
        if self.path.exists() or self.path.is_symlink():
            try:
                value = _read_json(self.path)
                if (value.get('schema_version') != 1 or value.get('session_id') != request.session_id
                        or type(value.get('known_zero_baseline')) is not bool
                        or type(value.get('created')) is not bool):
                    raise ValueError('invalid session accounting identity')
                for key in ('settled', 'reported'):
                    self.validate_counts(value[key])
                for key in ('seen', 'covered'):
                    for entry in value[key].values():self.validate_counts(entry)
                if value['models'] is not None:
                    for entry in value['models'].values():self.validate_counts(entry)
                for key in ('price_attributed', 'price_reported'):
                    if not _valid_cost(value[key]):raise ValueError('invalid stored price')
                if value['price_baseline'] is not None and not _valid_cost(value['price_baseline']):
                    raise ValueError('invalid stored price baseline')
                if not isinstance(value['warnings'], list) or any(not isinstance(w, str) for w in value['warnings']):
                    raise ValueError('invalid warnings')
                pending = zero()
                if not set(value['covered']).issubset(value['seen']):raise ValueError('unknown covered message')
                for identity, entry in value['seen'].items():
                    covered = value['covered'].get(identity, zero())
                    delta = {k:entry[k]-covered[k] for k in FIELDS}
                    if any(n < 0 for n in delta.values()):raise ValueError('invalid message coverage')
                    pending = add(pending, delta)
                if value['reported'] != add(value['settled'], pending) or value['price_attributed'] != value['price_reported']:
                    raise ValueError('invalid published accounting baseline')
                self.state = value
                self.loaded = True
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                # Never replace damaged/untrusted state with a guessed zero.
                self.state = empty(request.session_id, False)
                self.unwritable = True
                self.warn('invalid_session_accounting_state')
        self.before = dict(self.state['reported'])
        self.before_price = self.state['price_reported']
        self.initial = copy.deepcopy(self.state)
        if not self.state['known_zero_baseline']:
            self.warn('unknown_session_baseline')

    @staticmethod
    def validate_counts(value):
        if not isinstance(value, dict) or set(value) != set(FIELDS):raise ValueError('invalid stored counts')
        if any(type(value[k]) is not int or value[k] < 0 for k in FIELDS):raise ValueError('invalid stored count')
        if value['reasoning_tokens'] > value['output_tokens']:raise ValueError('invalid stored thinking')

    def warn(self, reason):
        if reason not in self.warnings:self.warnings.append(reason)

    def pending(self):
        total = zero()
        for identity, current in self.state['seen'].items():
            old = self.state['covered'].get(identity, zero())
            total = add(total, {key: current[key] - old[key] for key in FIELDS})
        return total

    def observe(self, event):
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            if event.get('session_id') == self.request.session_id and self.state['known_zero_baseline']:
                self.state['created'] = self.observed = True
            return
        if kind not in ('assistant', 'result'):return
        sid = event.get('session_id')
        if sid is not None and sid != self.request.session_id:
            self.warn('native_session_mismatch')
            return
        if kind == 'assistant':
            message = event.get('message')
            if not isinstance(message, dict) or not message.get('usage'):return
            self.observed = True
            try:current = counts(message['usage'], ASSISTANT, optional_cache=True)
            except (ValueError, TypeError, AttributeError):
                self.warn('invalid_assistant_usage')
                self.snapshot_valid = self.price_valid = False
                return
            if self.state['known_zero_baseline']:self.state['created'] = True
            # Assistant usage does not promise cumulative thinking coverage.
            # Retain a zero lower bound until the terminal model snapshot.
            current['reasoning_tokens'] = 0
            identity = message.get('id')
            if not isinstance(identity, str) or not identity:
                # Older streams cannot prove identity; a repeated usage tuple is
                # counted once as a provisional lower bound, never as complete.
                identity = 'unidentified:' + hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
                self.warn('assistant_message_id_missing')
            previous = self.state['seen'].get(identity, zero())
            if any(current[k] < previous[k] for k in FIELDS):
                self.warn('assistant_usage_decreased')
                return
            self.state['seen'][identity] = current
            if current != previous:
                self.snapshot_valid = self.price_valid = False
            return
        self.observed = True
        self.snapshot_valid = self.price_valid = False
        if event.get('subtype') == 'success' and not event.get('is_error') and self.state['known_zero_baseline']:
            self.state['created'] = True
        identity = event.get('uuid')
        signature = json.dumps([event.get('modelUsage'), event.get('total_cost_usd')], sort_keys=True)
        if isinstance(identity, str) and identity:
            if identity in self.terminals and self.terminals[identity] != signature:
                self.warn('conflicting_terminal_snapshot')
                return
            self.terminals[identity] = signature
        self.price(event.get('total_cost_usd'))
        raw = event.get('modelUsage')
        if not isinstance(raw, dict) or not raw:
            self.warn('terminal_model_usage_missing')
            return
        try:
            models = {name: counts(entry, NATIVE) for name, entry in raw.items()}
            old = self.state['models']
            if old is not None:
                for name, entry in old.items():
                    if name not in models or any(models[name][key] < entry[key] for key in PARTS):
                        raise ValueError('native cumulative counters decreased')
                    # Some CLI versions omit thinking on later snapshots. Keep
                    # the observed subset and disclose its incomplete coverage.
                    if 'thinkingTokens' not in raw[name]:
                        models[name]['reasoning_tokens'] = entry['reasoning_tokens']
                    elif models[name]['reasoning_tokens'] < entry['reasoning_tokens']:
                        raise ValueError('native cumulative thinking decreased')
            current = zero()
            previous = zero()
            for entry in models.values():current = add(current, entry)
            for entry in (old or {}).values():previous = add(previous, entry)
            pending = self.pending()
            if old is not None or self.state['known_zero_baseline']:
                delta = {key: current[key] - previous[key] for key in FIELDS}
                if any(delta[key] < pending[key] for key in PARTS):
                    raise ValueError('native snapshot does not cover observed messages')
            else:
                # First imported snapshot is only a baseline. This send's
                # deduplicated observations remain an explicit lower bound.
                delta = pending
            self.state['settled'] = add(self.state['settled'], delta)
            self.state['models'] = models
            self.state['covered'] = dict(self.state['seen'])
            self.snapshot_valid = True
            if self.state['known_zero_baseline']:self.state['created'] = True
            self.reasoning_coverage = all('thinkingTokens' in row for row in raw.values())
        except (ValueError, TypeError, AttributeError):
            self.warn('invalid_or_decreased_native_snapshot')

    def price(self, value):
        self.price_valid = False
        if not _valid_cost(value):
            self.warn('provider_estimated_price_missing_or_invalid')
            return
        previous = self.state['price_baseline']
        if previous is not None:
            if value < previous:
                self.warn('provider_estimated_price_decreased')
                return
            self.state['price_attributed'] += value - previous
            self.price_valid = True
        else:
            self.warn('unknown_price_baseline')
        self.state['price_baseline'] = value

    def apply(self, result):
        total = add(self.state['settled'], self.pending())
        delta = {key: total[key] - self.before[key] for key in FIELDS}
        if any(value < 0 for value in delta.values()):
            self.warn('unreconciled_negative_attribution')
            delta = zero() # Explicitly invalid; never publish a negative charge.
        result.usage = UsageBreakdown(**delta, total_tokens=sum(delta[k] for k in PARTS),
            total_cost=self.state['price_attributed'] - self.before_price)
        retrospective = max(0, result.usage.reasoning_tokens - result.usage.output_tokens)
        if retrospective:
            result.usage.reasoning_tokens -= retrospective
            self.warn('retrospective_reasoning_not_attributed_to_this_send')
        result.tokens = result.usage.total_tokens
        result.cost = result.usage.total_cost
        result.cost_known = self.price_valid and self.state['known_zero_baseline']
        warnings = list(dict.fromkeys([*self.state['warnings'], *self.warnings]))
        provisional = {'assistant_message_id_missing', 'terminal_model_usage_missing',
                       'provider_estimated_price_missing_or_invalid', 'invalid_assistant_usage'}
        result.accounting = {'schema_version': 1, 'basis': 'native_cumulative_session_delta' if self.snapshot_valid else 'provisional_unique_messages',
            'observed_native_usage': self.observed,
            'known_zero_baseline': self.state['known_zero_baseline'],
            'complete': self.snapshot_valid and self.state['known_zero_baseline'] and not (set(warnings) - provisional),
            'warnings': warnings,
            'provider_estimated_cost_usd': result.cost,
            'provider_estimated_cost_complete': result.cost_known,
            'retrospective_reasoning_tokens': retrospective,
            'native_session_reasoning_tokens': total['reasoning_tokens'],
            'reasoning_coverage_complete': self.reasoning_coverage and not retrospective,
            'actual_billed_subscription_cost_usd': None}

    def reject_creation(self):
        """An unsuccessful new-session lookup proves no zero baseline."""
        prior = self.initial
        has_prior_measurement = (prior['created'] or prior['models'] is not None
            or any(prior['reported'].values()) or prior['price_attributed'] > 0
            or (not prior['known_zero_baseline'] and prior['price_baseline'] is not None))
        if self.loaded and has_prior_measurement:
            # Discard the rejected lookup's counters, not prior owned work.
            self.state = copy.deepcopy(self.initial)
            self.warnings = ['fresh_session_creation_rejected']
            self.observed = self.snapshot_valid = self.price_valid = False
            return
        self.state = empty(self.request.session_id, False)
        self.snapshot_valid = self.price_valid = False
        self.warn('fresh_session_creation_rejected')
        self.warn('unknown_session_baseline')

    def defer(self, result):
        """Do not charge a delta that a later resume could charge again."""
        result.accounting['unattributed_usage'] = result.usage.model_dump()
        result.accounting['unattributed_provider_estimated_cost_usd'] = result.cost
        result.usage = UsageBreakdown()
        result.tokens = 0
        result.cost = 0.0
        result.cost_known = False
        result.accounting.update(complete=False, provider_estimated_cost_complete=False,
                                 provider_estimated_cost_usd=0.0)

    def finish(self, result, *, interrupted=False):
        self.apply(result)
        if self.observed and not self.unwritable:
            self.state['reported'] = add(self.state['settled'], self.pending())
            self.state['price_reported'] = self.state['price_attributed']
            # Provisional identity/coverage warnings are superseded by an exact
            # terminal snapshot. Baseline and counter anomalies remain visible.
            self.state['warnings'] = [w for w in result.accounting['warnings'] if w not in (
                'assistant_message_id_missing', 'terminal_model_usage_missing',
                'provider_estimated_price_missing_or_invalid')]
            try:_atomic_json(self.path, self.state)
            except (OSError, ValueError, RuntimeError):
                self.warn('session_accounting_state_not_persisted')
                self.apply(result)
                self.defer(result)
        elif self.unwritable:
            self.defer(result)
        if interrupted:
            result.cost_known = False
            result.accounting.update(complete=False, provider_estimated_cost_complete=False,
                                     interrupted=True)
        write_receipt(self.request, result)
